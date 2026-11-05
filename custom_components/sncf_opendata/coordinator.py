"""Data handling of the SNCF Open Data integration."""

from __future__ import annotations

from collections.abc import Iterable
import contextlib
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import logging
import os
from typing import Any, override

import aiohttp
from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    ALERTS_REFRESH_INTERVAL,
    CONF_DELAY_THRESHOLD,
    CONF_DESTINATIONS,
    CONF_INCLUDE_COACHES,
    CONF_MAX_TRAINS,
    CONF_ORIGIN,
    CONF_PLATFORMS,
    CONF_REALTIME_INTERVAL,
    CONF_TIME_END,
    CONF_TIME_START,
    CONF_WEEKDAYS,
    DEFAULT_DELAY_THRESHOLD,
    DEFAULT_INCLUDE_COACHES,
    DEFAULT_MAX_TRAINS,
    DEFAULT_REALTIME_INTERVAL,
    DEPARTED_GRACE,
    DOMAIN,
    HOT_AFTER_WINDOW,
    HOT_BEFORE_WINDOW,
    HTTP_TIMEOUT,
    IDLE_UPDATE_INTERVAL,
    REALTIME_MAX_AGE,
    STATIC_HTTP_TIMEOUT,
    STORAGE_KEY,
    STORAGE_VERSION,
    SUBENTRY_TYPE_ROUTE,
    TZ_PARIS,
    URL_GTFS_RT_SERVICE_ALERTS,
    URL_GTFS_RT_TRIP_UPDATES,
    URL_GTFS_STATIC,
    WEEKDAYS,
)
from .gtfs_realtime import (
    Alerts,
    RealtimeFormatError,
    TripUpdates,
    parse_alerts,
    parse_trip_updates,
)
from .gtfs_static import GtfsFormatError, IndexFormatError, StaticIndex, parse_gtfs
from .platforms import PlatformClient, PlatformMap
from .timetable import RouteConfig, RouteSnapshot, build_snapshot

_LOGGER = logging.getLogger(__name__)

# The index covers from yesterday (trips after midnight) to a week ahead.
INDEX_DAYS_BEFORE = 1
INDEX_DAYS_AFTER = 8


@dataclass(slots=True)
class SncfRuntimeData:
    """Runtime data of the config entry."""

    static: StaticTimetable
    coordinator: SncfCoordinator


type SncfConfigEntry = ConfigEntry[SncfRuntimeData]


class StaticUnavailableError(Exception):
    """The timetable could neither be downloaded nor read from the cache."""


def route_from_subentry(subentry: ConfigSubentry) -> RouteConfig:
    """Build the route followed by a subentry."""
    data = subentry.data
    return RouteConfig(
        route_id=subentry.subentry_id,
        name=subentry.title,
        origin=data[CONF_ORIGIN],
        destinations=tuple(data[CONF_DESTINATIONS]),
        time_start=time.fromisoformat(data[CONF_TIME_START]),
        time_end=time.fromisoformat(data[CONF_TIME_END]),
        weekdays=frozenset(WEEKDAYS.index(day) for day in data[CONF_WEEKDAYS]),
        delay_threshold=int(data.get(CONF_DELAY_THRESHOLD, DEFAULT_DELAY_THRESHOLD)),
        max_trains=int(data.get(CONF_MAX_TRAINS, DEFAULT_MAX_TRAINS)),
        include_coaches=bool(data.get(CONF_INCLUDE_COACHES, DEFAULT_INCLUDE_COACHES)),
    )


def entry_routes(entry: ConfigEntry) -> dict[str, RouteConfig]:
    """Return the routes of a config entry, by subentry id."""
    return {
        subentry_id: route_from_subentry(subentry)
        for subentry_id, subentry in entry.subentries.items()
        if subentry.subentry_type == SUBENTRY_TYPE_ROUTE
    }


def routes_uics(routes: Iterable[RouteConfig]) -> set[str]:
    """Return every station used by the routes."""
    return {uic for route in routes for uic in (route.origin, *route.destinations)}


def _index_range(today: date) -> tuple[date, date]:
    return (
        today - timedelta(days=INDEX_DAYS_BEFORE),
        today + timedelta(days=INDEX_DAYS_AFTER),
    )


def _write_file(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "wb") as file:
        file.write(data)
    os.replace(tmp, path)


def _remove_file(path: str) -> None:
    with contextlib.suppress(FileNotFoundError):
        os.remove(path)


class StaticTimetable:
    """The planned timetable: downloaded, indexed, and cached on disk.

    The national archive is kept in the cache directory (not backed up), so
    that adding a route only needs a new pass over it, not a new download. The
    small index of the stations in use is stored with the other integration
    data and survives restarts.
    """

    def __init__(self, hass: HomeAssistant, session: aiohttp.ClientSession) -> None:
        """Initialize."""
        self._hass = hass
        self._session = session
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._zip_path = hass.config.cache_path(DOMAIN, "gtfs.zip")
        self.index: StaticIndex | None = None
        self._etag: str | None = None
        self._last_modified: str | None = None
        self.checked_at: datetime | None = None

    async def async_load(self, uics: set[str]) -> None:
        """Make the index of these stations available.

        Raise StaticUnavailableError when there is no way to get it.
        """
        stored = await self._store.async_load()
        if stored:
            self._etag = stored.get("etag")
            self._last_modified = stored.get("last_modified")
            try:
                self.index = StaticIndex.from_dict(stored["index"])
            except IndexFormatError as err:
                _LOGGER.debug("Rebuilding the timetable index: %s", err)
                self.index = None
            except (KeyError, ValueError) as err:
                _LOGGER.warning("Ignoring the stored timetable index: %s", err)
                self.index = None
            checked = stored.get("checked_at")
            self.checked_at = dt_util.parse_datetime(checked) if checked else None
        today = dt_util.now(TZ_PARIS).date()
        first, last = _index_range(today)
        fresh = (
            self.checked_at is not None
            and dt_util.utcnow() - self.checked_at < timedelta(hours=12)
        )
        if (
            self.index is not None
            and fresh
            and self.index.covers(uics, first, last - timedelta(days=1))
        ):
            return
        await self.async_refresh(uics)
        if self.index is None:
            raise StaticUnavailableError("No timetable available")

    async def async_refresh(self, uics: set[str]) -> bool:
        """Download the timetable if it changed and rebuild the index if needed.

        Return True when the index changed. Never raises: on failure the
        previous index (if any) is kept.
        """
        downloaded = await self._async_download()
        have_zip = await self._hass.async_add_executor_job(
            os.path.exists, self._zip_path
        )
        today = dt_util.now(TZ_PARIS).date()
        first, last = _index_range(today)
        needs_parse = downloaded or (
            self.index is None
            or not self.index.covers(uics, first, last - timedelta(days=1))
        )
        if not needs_parse:
            await self._async_save()
            return False
        if not have_zip:
            if self.index is not None:
                _LOGGER.warning(
                    "Timetable could not be downloaded, keeping the previous one"
                )
            return False
        try:
            index = await self._hass.async_add_executor_job(
                parse_gtfs, self._zip_path, uics, first, last
            )
        except (GtfsFormatError, OSError, ValueError) as err:
            _LOGGER.error("Could not read the SNCF timetable: %s", err)
            await self._hass.async_add_executor_job(_remove_file, self._zip_path)
            self._etag = self._last_modified = None
            return False
        _LOGGER.debug(
            "Timetable %s indexed: %d trips for %d stations",
            index.feed_version,
            len(index.trips),
            len(index.uics),
        )
        self.index = index
        await self._async_save()
        return True

    async def _async_download(self) -> bool:
        """Download the archive when it changed. Return True if a new one came."""
        headers = {}
        have_zip = await self._hass.async_add_executor_job(
            os.path.exists, self._zip_path
        )
        if have_zip:
            if self._etag:
                headers["If-None-Match"] = self._etag
            if self._last_modified:
                headers["If-Modified-Since"] = self._last_modified
        try:
            async with self._session.get(
                URL_GTFS_STATIC,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=STATIC_HTTP_TIMEOUT),
            ) as response:
                if response.status == 304:
                    self.checked_at = dt_util.utcnow()
                    return False
                response.raise_for_status()
                data = await response.read()
                etag = response.headers.get("ETag")
                last_modified = response.headers.get("Last-Modified")
        except (aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.warning("Could not download the SNCF timetable: %s", err)
            return False
        await self._hass.async_add_executor_job(_write_file, self._zip_path, data)
        self._etag, self._last_modified = etag, last_modified
        self.checked_at = dt_util.utcnow()
        return True

    async def _async_save(self) -> None:
        if self.index is None:
            return
        await self._store.async_save(
            {
                "etag": self._etag,
                "last_modified": self._last_modified,
                "checked_at": self.checked_at.isoformat() if self.checked_at else None,
                "index": self.index.as_dict(),
            }
        )

    async def async_remove(self) -> None:
        """Delete the stored index and the cached archive."""
        await self._store.async_remove()
        await self._hass.async_add_executor_job(_remove_file, self._zip_path)


class SncfCoordinator(DataUpdateCoordinator[dict[str, RouteSnapshot]]):
    """Compute the departures of every route.

    The realtime feeds are national: they are fetched once for all routes,
    and only around the time windows of the routes, to save bandwidth.
    """

    config_entry: SncfConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: SncfConfigEntry,
        static: StaticTimetable,
        session: aiohttp.ClientSession,
    ) -> None:
        """Initialize."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=DOMAIN,
            update_interval=IDLE_UPDATE_INTERVAL,
        )
        self.static = static
        self._session = session
        self.routes = entry_routes(config_entry)
        options = config_entry.options
        self._realtime_interval = timedelta(
            seconds=int(options.get(CONF_REALTIME_INTERVAL, DEFAULT_REALTIME_INTERVAL))
        )
        self.platforms: PlatformClient | None = (
            PlatformClient(session) if options.get(CONF_PLATFORMS, False) else None
        )
        self.trip_updates: TripUpdates | None = None
        self.alerts: Alerts | None = None
        self._alerts_fetched_at: datetime | None = None
        # None until the realtime feed has been needed once.
        self.realtime_available: bool | None = None
        self.realtime_error: str | None = None

    def hot_routes(self, now: datetime) -> list[RouteConfig]:
        """Return the routes whose time window is close or open."""
        hot = []
        for route in self.routes.values():
            window = route.window(now)
            if window is None:
                continue
            start, end = window
            if start - HOT_BEFORE_WINDOW <= now <= end + HOT_AFTER_WINDOW:
                hot.append(route)
        return hot

    async def _async_fetch(self, url: str) -> bytes:
        async with self._session.get(
            url, timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT)
        ) as response:
            response.raise_for_status()
            return await response.read()

    async def _async_trip_updates(self, now: datetime) -> TripUpdates | None:
        """Refresh the trip updates. Return them only if recent enough."""
        error: str | None = None
        try:
            data = await self._async_fetch(URL_GTFS_RT_TRIP_UPDATES)
            self.trip_updates = await self.hass.async_add_executor_job(
                parse_trip_updates, data
            )
        except (aiohttp.ClientError, TimeoutError, RealtimeFormatError) as err:
            error = str(err) or type(err).__name__
        updates = self.trip_updates
        if updates is not None and now - updates.timestamp <= REALTIME_MAX_AGE:
            # A failed fetch is fine while the previous feed is still recent.
            if self.realtime_available is False:
                _LOGGER.info("SNCF realtime feed is available again")
            self.realtime_available = True
            self.realtime_error = error
            return updates
        if error is None:
            error = (
                f"feed out of date ({updates.timestamp.isoformat()})"
                if updates
                else "no data"
            )
        if self.realtime_available is not False:
            _LOGGER.warning(
                "SNCF realtime feed unavailable: %s. Planned times are shown "
                "until it is back",
                error,
            )
        self.realtime_available = False
        self.realtime_error = error
        return None

    async def _async_alerts(self, now: datetime) -> Alerts | None:
        """Refresh the alerts every few minutes. They are optional."""
        if (
            self._alerts_fetched_at is None
            or now - self._alerts_fetched_at >= ALERTS_REFRESH_INTERVAL
        ):
            self._alerts_fetched_at = now
            language = self.hass.config.language or "fr"
            try:
                data = await self._async_fetch(URL_GTFS_RT_SERVICE_ALERTS)
                self.alerts = await self.hass.async_add_executor_job(
                    parse_alerts, data, language
                )
            except (aiohttp.ClientError, TimeoutError, RealtimeFormatError) as err:
                _LOGGER.debug("SNCF alerts feed unavailable: %s", err)
        if self.alerts is not None and now - self.alerts.timestamp > timedelta(hours=1):
            return None
        return self.alerts

    @override
    async def _async_update_data(self) -> dict[str, RouteSnapshot]:
        """Compute the snapshots of all routes."""
        index = self.static.index
        if index is None:
            raise UpdateFailed(
                translation_domain=DOMAIN, translation_key="timetable_unavailable"
            )
        now = dt_util.utcnow()
        hot = self.hot_routes(now)
        updates: TripUpdates | None = None
        alerts: Alerts | None = None
        platforms: dict[str, PlatformMap] = {}
        if hot:
            self.update_interval = self._realtime_interval
            updates = await self._async_trip_updates(now)
            alerts = await self._async_alerts(now)
            if self.platforms is not None:
                for origin in {route.origin for route in hot}:
                    platforms[origin] = await self.platforms.async_platforms(
                        origin, now
                    )
        else:
            self.update_interval = IDLE_UPDATE_INTERVAL
            # Nothing to follow now: free the national feeds.
            self.trip_updates = None
            self.alerts = None
            self._alerts_fetched_at = None
        hot_ids = {route.route_id for route in hot}
        return {
            route_id: build_snapshot(
                route,
                index,
                updates if route_id in hot_ids else None,
                alerts if route_id in hot_ids else None,
                platforms.get(route.origin),
                now,
                DEPARTED_GRACE,
            )
            for route_id, route in self.routes.items()
        }
