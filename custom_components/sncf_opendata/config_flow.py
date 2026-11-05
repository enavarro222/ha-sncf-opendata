"""Config flow of the SNCF Open Data integration."""

from __future__ import annotations

from datetime import time
import logging
from typing import Any, override

import aiohttp
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TimeSelector,
)
import probatio

from .const import (
    CONF_DELAY_THRESHOLD,
    CONF_DESTINATIONS,
    CONF_DESTINATIONS_QUERY,
    CONF_INCLUDE_COACHES,
    CONF_MAX_TRAINS,
    CONF_ORIGIN,
    CONF_ORIGIN_QUERY,
    CONF_PLATFORMS,
    CONF_REALTIME_INTERVAL,
    CONF_ROUTE_NAME,
    CONF_TIME_END,
    CONF_TIME_START,
    CONF_WEEKDAYS,
    DEFAULT_DELAY_THRESHOLD,
    DEFAULT_INCLUDE_COACHES,
    DEFAULT_MAX_TRAINS,
    DEFAULT_REALTIME_INTERVAL,
    DEFAULT_TIME_END,
    DEFAULT_TIME_START,
    DEFAULT_WEEKDAYS,
    DOMAIN,
    HTTP_TIMEOUT,
    MAX_REALTIME_INTERVAL,
    MIN_REALTIME_INTERVAL,
    SUBENTRY_TYPE_ROUTE,
    URL_GTFS_RT_TRIP_UPDATES,
    WEEKDAYS,
)
from .coordinator import SncfRuntimeData
from .gtfs_realtime import RealtimeFormatError, parse_trip_updates
from .gtfs_static import StaticIndex, Station

_LOGGER = logging.getLogger(__name__)

OPTIONS_SCHEMA = probatio.Schema(
    {
        probatio.Required(
            CONF_REALTIME_INTERVAL, default=DEFAULT_REALTIME_INTERVAL
        ): NumberSelector(
            NumberSelectorConfig(
                min=MIN_REALTIME_INTERVAL,
                max=MAX_REALTIME_INTERVAL,
                step=30,
                unit_of_measurement="s",
                mode=NumberSelectorMode.BOX,
            )
        ),
        probatio.Required(CONF_PLATFORMS, default=False): BooleanSelector(),
    }
)


class SncfOpenDataConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up the SNCF open data feeds (one entry; routes are subentries)."""

    VERSION = 1
    MINOR_VERSION = 1

    @classmethod
    @callback
    @override
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Return the subentries of the integration."""
        return {SUBENTRY_TYPE_ROUTE: RouteSubentryFlow}

    @staticmethod
    @callback
    @override
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow."""
        return SncfOptionsFlow()

    @override
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Check that the SNCF realtime feed answers, then create the entry."""
        errors: dict[str, str] = {}
        if user_input is not None:
            session = async_get_clientsession(self.hass)
            try:
                async with session.get(
                    URL_GTFS_RT_TRIP_UPDATES,
                    timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT),
                ) as response:
                    response.raise_for_status()
                    data = await response.read()
                await self.hass.async_add_executor_job(parse_trip_updates, data)
            except aiohttp.ClientError, TimeoutError:
                errors["base"] = "cannot_connect"
            except RealtimeFormatError:
                errors["base"] = "invalid_feed"
            except Exception:
                _LOGGER.exception("Unexpected error checking the SNCF feed")
                errors["base"] = "unknown"
            else:
                return self.async_create_entry(
                    title="SNCF Open Data",
                    data={},
                    options={
                        CONF_REALTIME_INTERVAL: DEFAULT_REALTIME_INTERVAL,
                        CONF_PLATFORMS: False,
                    },
                )
        return self.async_show_form(
            step_id="user", data_schema=probatio.Schema({}), errors=errors
        )


class SncfOptionsFlow(OptionsFlow):
    """Options of the feeds."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(
                data={
                    CONF_REALTIME_INTERVAL: int(user_input[CONF_REALTIME_INTERVAL]),
                    CONF_PLATFORMS: bool(user_input[CONF_PLATFORMS]),
                }
            )
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                OPTIONS_SCHEMA, self.config_entry.options
            ),
        )


def _station_options(stations: list[Station]) -> list[SelectOptionDict]:
    return [SelectOptionDict(value=s.uic, label=s.name) for s in stations]


def _parse_time(value: str) -> time:
    return time.fromisoformat(value)


class RouteSubentryFlow(ConfigSubentryFlow):
    """Add or change a route: an origin, destinations and a time window."""

    def __init__(self) -> None:
        """Initialize."""
        self._origins: list[Station] = []
        self._destinations: list[Station] = []
        self._data: dict[str, Any] = {}

    def _index(self) -> StaticIndex | None:
        entry = self._get_entry()
        if entry.state is not ConfigEntryState.LOADED:
            return None
        runtime: SncfRuntimeData = entry.runtime_data
        return runtime.static.index

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Search the stations by name."""
        index = self._index()
        if index is None:
            return self.async_abort(reason="entry_not_loaded")
        errors: dict[str, str] = {}
        if user_input is not None:
            self._origins = index.search_stations(user_input[CONF_ORIGIN_QUERY])
            seen: set[str] = set()
            self._destinations = []
            for query in user_input[CONF_DESTINATIONS_QUERY].split(","):
                for station in index.search_stations(query):
                    if station.uic not in seen:
                        seen.add(station.uic)
                        self._destinations.append(station)
            if not self._origins:
                errors[CONF_ORIGIN_QUERY] = "station_not_found"
            if not self._destinations:
                errors[CONF_DESTINATIONS_QUERY] = "station_not_found"
            if not errors:
                return await self.async_step_stations()
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                probatio.Schema(
                    {
                        probatio.Required(CONF_ORIGIN_QUERY): TextSelector(),
                        probatio.Required(CONF_DESTINATIONS_QUERY): TextSelector(),
                    }
                ),
                user_input or {},
            ),
            errors=errors,
        )

    async def async_step_stations(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Pick the stations among the search results."""
        errors: dict[str, str] = {}
        if user_input is not None:
            origin = user_input[CONF_ORIGIN]
            destinations = [d for d in user_input[CONF_DESTINATIONS] if d != origin]
            if not destinations:
                errors[CONF_DESTINATIONS] = "same_station"
            else:
                self._data[CONF_ORIGIN] = origin
                self._data[CONF_DESTINATIONS] = destinations
                return await self.async_step_schedule()
        return self.async_show_form(
            step_id="stations",
            data_schema=probatio.Schema(
                {
                    probatio.Required(
                        CONF_ORIGIN, default=self._origins[0].uic
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=_station_options(self._origins),
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    ),
                    probatio.Required(
                        CONF_DESTINATIONS, default=[self._destinations[0].uic]
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=_station_options(self._destinations),
                            multiple=True,
                            mode=SelectSelectorMode.LIST,
                        )
                    ),
                }
            ),
            errors=errors,
        )

    def _default_name(self) -> str:
        index = self._index()
        names = {s.uic: s.name for s in [*self._origins, *self._destinations]}
        if index is not None:
            for uic in (self._data[CONF_ORIGIN], *self._data[CONF_DESTINATIONS]):
                names.setdefault(uic, index.station_name(uic) or uic)
        origin = names.get(self._data[CONF_ORIGIN], self._data[CONF_ORIGIN])
        destinations = " / ".join(
            names.get(uic, uic) for uic in self._data[CONF_DESTINATIONS]
        )
        return f"{origin} → {destinations}"

    def _schedule_schema(self, defaults: dict[str, Any]) -> probatio.Schema:
        return probatio.Schema(
            {
                probatio.Required(
                    CONF_ROUTE_NAME, default=defaults[CONF_ROUTE_NAME]
                ): TextSelector(),
                probatio.Required(
                    CONF_TIME_START, default=defaults[CONF_TIME_START]
                ): TimeSelector(),
                probatio.Required(
                    CONF_TIME_END, default=defaults[CONF_TIME_END]
                ): TimeSelector(),
                probatio.Required(
                    CONF_WEEKDAYS, default=defaults[CONF_WEEKDAYS]
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=list(WEEKDAYS),
                        multiple=True,
                        mode=SelectSelectorMode.LIST,
                        translation_key=CONF_WEEKDAYS,
                    )
                ),
                probatio.Required(
                    CONF_DELAY_THRESHOLD, default=defaults[CONF_DELAY_THRESHOLD]
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=1,
                        max=60,
                        step=1,
                        unit_of_measurement="min",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
                probatio.Required(
                    CONF_MAX_TRAINS, default=defaults[CONF_MAX_TRAINS]
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=1, max=20, step=1, mode=NumberSelectorMode.BOX
                    )
                ),
                probatio.Required(
                    CONF_INCLUDE_COACHES, default=defaults[CONF_INCLUDE_COACHES]
                ): BooleanSelector(),
            }
        )

    def _validate_schedule(
        self, user_input: dict[str, Any], current_id: str | None
    ) -> tuple[dict[str, Any], dict[str, str], str]:
        """Check the schedule step. Return (data, errors, unique id)."""
        errors: dict[str, str] = {}
        start = _parse_time(user_input[CONF_TIME_START])
        end = _parse_time(user_input[CONF_TIME_END])
        if end <= start:
            errors[CONF_TIME_END] = "end_before_start"
        if not user_input[CONF_WEEKDAYS]:
            errors[CONF_WEEKDAYS] = "no_weekday"
        data = {
            CONF_ORIGIN: self._data[CONF_ORIGIN],
            CONF_DESTINATIONS: list(self._data[CONF_DESTINATIONS]),
            CONF_TIME_START: start.isoformat(),
            CONF_TIME_END: end.isoformat(),
            CONF_WEEKDAYS: [d for d in WEEKDAYS if d in user_input[CONF_WEEKDAYS]],
            CONF_DELAY_THRESHOLD: int(user_input[CONF_DELAY_THRESHOLD]),
            CONF_MAX_TRAINS: int(user_input[CONF_MAX_TRAINS]),
            CONF_INCLUDE_COACHES: bool(user_input[CONF_INCLUDE_COACHES]),
        }
        unique_id = (
            f"{data[CONF_ORIGIN]}>{'+'.join(sorted(data[CONF_DESTINATIONS]))}"
            f"@{data[CONF_TIME_START]}-{data[CONF_TIME_END]}"
        )
        for subentry_id, subentry in self._get_entry().subentries.items():
            if subentry_id != current_id and subentry.unique_id == unique_id:
                errors["base"] = "already_configured"
        return data, errors, unique_id

    async def async_step_schedule(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Set the name, time window and days of the route."""
        errors: dict[str, str] = {}
        if user_input is not None:
            data, errors, unique_id = self._validate_schedule(user_input, None)
            if not errors:
                return self.async_create_entry(
                    title=user_input[CONF_ROUTE_NAME].strip() or self._default_name(),
                    data=data,
                    unique_id=unique_id,
                )
        defaults = user_input or {
            CONF_ROUTE_NAME: self._default_name(),
            CONF_TIME_START: DEFAULT_TIME_START,
            CONF_TIME_END: DEFAULT_TIME_END,
            CONF_WEEKDAYS: DEFAULT_WEEKDAYS,
            CONF_DELAY_THRESHOLD: DEFAULT_DELAY_THRESHOLD,
            CONF_MAX_TRAINS: DEFAULT_MAX_TRAINS,
            CONF_INCLUDE_COACHES: DEFAULT_INCLUDE_COACHES,
        }
        return self.async_show_form(
            step_id="schedule",
            data_schema=self._schedule_schema(defaults),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Change the name, time window and days of a route."""
        subentry = self._get_reconfigure_subentry()
        self._data = {
            CONF_ORIGIN: subentry.data[CONF_ORIGIN],
            CONF_DESTINATIONS: subentry.data[CONF_DESTINATIONS],
        }
        errors: dict[str, str] = {}
        if user_input is not None:
            data, errors, unique_id = self._validate_schedule(
                user_input, subentry.subentry_id
            )
            if not errors:
                return self.async_update_and_abort(
                    self._get_entry(),
                    subentry,
                    title=user_input[CONF_ROUTE_NAME].strip() or subentry.title,
                    data=data,
                    unique_id=unique_id,
                )
        defaults = user_input or {
            CONF_ROUTE_NAME: subentry.title,
            CONF_TIME_START: subentry.data[CONF_TIME_START],
            CONF_TIME_END: subentry.data[CONF_TIME_END],
            CONF_WEEKDAYS: subentry.data[CONF_WEEKDAYS],
            CONF_DELAY_THRESHOLD: subentry.data.get(
                CONF_DELAY_THRESHOLD, DEFAULT_DELAY_THRESHOLD
            ),
            CONF_MAX_TRAINS: subentry.data.get(CONF_MAX_TRAINS, DEFAULT_MAX_TRAINS),
            CONF_INCLUDE_COACHES: subentry.data.get(
                CONF_INCLUDE_COACHES, DEFAULT_INCLUDE_COACHES
            ),
        }
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self._schedule_schema(defaults),
            errors=errors,
        )
