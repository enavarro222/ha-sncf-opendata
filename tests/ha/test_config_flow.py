"""Tests of the config, options and route flows."""

from __future__ import annotations

from collections.abc import Callable
from unittest.mock import patch

from aiohttp import ClientError
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import SOURCE_RECONFIGURE, SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sncf_opendata.const import (
    CONF_DELAY_THRESHOLD,
    CONF_DESTINATIONS,
    CONF_DESTINATIONS_QUERY,
    CONF_MAX_TRAINS,
    CONF_ORIGIN,
    CONF_ORIGIN_QUERY,
    CONF_PLATFORMS,
    CONF_REALTIME_INTERVAL,
    CONF_ROUTE_NAME,
    CONF_TIME_END,
    CONF_TIME_START,
    CONF_WEEKDAYS,
    DOMAIN,
    SUBENTRY_TYPE_ROUTE,
)
from tests.builders import A, B, C, D

from .conftest import Feeds

SCHEDULE = {
    CONF_ROUTE_NAME: "Aller",
    CONF_TIME_START: "07:00:00",
    CONF_TIME_END: "09:00:00",
    CONF_WEEKDAYS: ["mon", "fri"],
    CONF_DELAY_THRESHOLD: 5,
    CONF_MAX_TRAINS: 4,
}


async def test_user_flow(hass: HomeAssistant, feeds: Feeds) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    with patch("custom_components.sncf_opendata.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "SNCF Open Data"
    assert result["options"] == {CONF_REALTIME_INTERVAL: 120, CONF_PLATFORMS: False}

    # One entry only.
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"


async def test_user_flow_errors(hass: HomeAssistant, feeds: Feeds) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    feeds.rt_status = 500
    feeds.install()
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["errors"] == {"base": "cannot_connect"}

    feeds.rt_status = 200
    feeds.rt_content = b"\xff\xff garbage"
    feeds.install()
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["errors"] == {"base": "invalid_feed"}

    with patch(
        "custom_components.sncf_opendata.config_flow.parse_trip_updates",
        side_effect=RuntimeError,
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["errors"] == {"base": "unknown"}

    feeds.mock.clear_requests()
    feeds.mock.get(
        "https://proxy.transport.data.gouv.fr/resource/sncf-gtfs-rt-trip-updates",
        exc=ClientError(),
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["errors"] == {"base": "cannot_connect"}


async def test_options_flow(
    hass: HomeAssistant,
    feeds: Feeds,
    at_16h: FrozenDateTimeFactory,
    make_entry: Callable[..., MockConfigEntry],
) -> None:
    feeds.board = []
    feeds.install()
    entry = make_entry()
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_REALTIME_INTERVAL: 300.0, CONF_PLATFORMS: True}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options == {CONF_REALTIME_INTERVAL: 300, CONF_PLATFORMS: True}
    # Reloaded with the new options.
    assert entry.runtime_data.coordinator.platforms is not None


async def start_route_flow(hass: HomeAssistant, entry: MockConfigEntry):
    return await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_ROUTE), context={"source": SOURCE_USER}
    )


async def test_route_flow(
    hass: HomeAssistant,
    feeds: Feeds,
    at_16h: FrozenDateTimeFactory,
    make_entry: Callable[..., MockConfigEntry],
) -> None:
    entry = make_entry(routes=0)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await start_route_flow(hass, entry)
    assert result["step_id"] == "user"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {CONF_ORIGIN_QUERY: "nowhere", CONF_DESTINATIONS_QUERY: "nothing"},
    )
    assert result["errors"] == {
        CONF_ORIGIN_QUERY: "station_not_found",
        CONF_DESTINATIONS_QUERY: "station_not_found",
    }
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            CONF_ORIGIN_QUERY: "matabiau",
            CONF_DESTINATIONS_QUERY: "Rabastens, St Sulpice, rabastens, Toulouse",
        },
    )
    assert result["step_id"] == "stations"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_ORIGIN: A, CONF_DESTINATIONS: [A]}
    )
    assert result["errors"] == {CONF_DESTINATIONS: "same_station"}
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_ORIGIN: A, CONF_DESTINATIONS: [C, B]}
    )
    assert result["step_id"] == "schedule"
    defaults = {key.schema: key.default() for key in result["data_schema"].schema}
    assert defaults[CONF_ROUTE_NAME] == (
        "Toulouse Matabiau → Rabastens - Couffouleux / Saint-Sulpice"
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        SCHEDULE | {CONF_TIME_END: "06:00:00", CONF_WEEKDAYS: []},
    )
    assert result["errors"] == {
        CONF_TIME_END: "end_before_start",
        CONF_WEEKDAYS: "no_weekday",
    }
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], SCHEDULE
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    (subentry,) = entry.subentries.values()
    assert subentry.title == "Aller"
    assert subentry.data == {
        CONF_ORIGIN: A,
        CONF_DESTINATIONS: [C, B],
        CONF_TIME_START: "07:00:00",
        CONF_TIME_END: "09:00:00",
        CONF_WEEKDAYS: ["mon", "fri"],
        CONF_DELAY_THRESHOLD: 5,
        CONF_MAX_TRAINS: 4,
    }
    # The entry was reloaded and follows the new route.
    assert subentry.subentry_id in entry.runtime_data.coordinator.routes

    # The same route again is refused.
    result = await start_route_flow(hass, entry)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {CONF_ORIGIN_QUERY: "matabiau", CONF_DESTINATIONS_QUERY: "rabastens, sulpice"},
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_ORIGIN: A, CONF_DESTINATIONS: [B, C]}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], SCHEDULE
    )
    assert result["errors"] == {"base": "already_configured"}


async def test_route_reconfigure(
    hass: HomeAssistant,
    feeds: Feeds,
    at_16h: FrozenDateTimeFactory,
    make_entry: Callable[..., MockConfigEntry],
) -> None:
    entry = make_entry()
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    subentry_id = next(iter(entry.subentries))
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_ROUTE),
        context={"source": SOURCE_RECONFIGURE, "subentry_id": subentry_id},
    )
    assert result["step_id"] == "reconfigure"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        SCHEDULE
        | {
            CONF_ROUTE_NAME: "  ",
            CONF_TIME_START: "16:00:00",
            CONF_TIME_END: "19:00:00",
        },
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    subentry = entry.subentries[subentry_id]
    assert subentry.title == "Retour"
    assert subentry.data[CONF_TIME_START] == "16:00:00"
    assert subentry.data[CONF_DESTINATIONS] == [C, B]
    route = entry.runtime_data.coordinator.routes[subentry_id]
    assert route.time_end.isoformat() == "19:00:00"


async def test_route_flow_needs_loaded_entry(
    hass: HomeAssistant, make_entry: Callable[..., MockConfigEntry]
) -> None:
    entry = make_entry(routes=0)
    result = await start_route_flow(hass, entry)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "entry_not_loaded"


async def test_destination_search_keeps_order(
    hass: HomeAssistant,
    feeds: Feeds,
    at_16h: FrozenDateTimeFactory,
    make_entry: Callable[..., MockConfigEntry],
) -> None:
    entry = make_entry(routes=0)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    result = await start_route_flow(hass, entry)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {CONF_ORIGIN_QUERY: "toulouse", CONF_DESTINATIONS_QUERY: "albi"},
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_ORIGIN: A, CONF_DESTINATIONS: [D]}
    )
    assert result["step_id"] == "schedule"
