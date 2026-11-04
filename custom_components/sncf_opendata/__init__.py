"""The SNCF Open Data integration."""

from __future__ import annotations

from datetime import datetime

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval

from .const import DOMAIN, STATIC_REFRESH_INTERVAL
from .coordinator import (
    SncfConfigEntry,
    SncfCoordinator,
    SncfRuntimeData,
    StaticTimetable,
    StaticUnavailableError,
    entry_routes,
    routes_uics,
)

PLATFORMS = [Platform.BINARY_SENSOR, Platform.EVENT, Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: SncfConfigEntry) -> bool:
    """Set up SNCF Open Data from a config entry."""
    session = async_get_clientsession(hass)
    static = StaticTimetable(hass, session)
    uics = routes_uics(entry_routes(entry).values())
    try:
        await static.async_load(uics)
    except StaticUnavailableError as err:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN, translation_key="timetable_unavailable"
        ) from err

    coordinator = SncfCoordinator(hass, entry, static, session)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = SncfRuntimeData(static=static, coordinator=coordinator)

    async def _async_refresh_timetable(_now: datetime) -> None:
        if await static.async_refresh(uics):
            await coordinator.async_request_refresh()

    entry.async_on_unload(
        async_track_time_interval(
            hass,
            _async_refresh_timetable,
            STATIC_REFRESH_INTERVAL,
            name="SNCF timetable refresh",
            cancel_on_shutdown=True,
        )
    )
    # Routes are subentries: adding, changing or removing one reloads the entry.
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_update_listener(hass: HomeAssistant, entry: SncfConfigEntry) -> None:
    """Reload when options or routes change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: SncfConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: SncfConfigEntry) -> None:
    """Remove the cached timetable when the integration is removed."""
    await StaticTimetable(hass, async_get_clientsession(hass)).async_remove()
