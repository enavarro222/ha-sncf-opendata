"""Tests of the setup, entities and events."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
import os

from aiohttp import ClientError
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNKNOWN
from homeassistant.core import Event, EventStateChangedData, HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.sncf_opendata.const import (
    DOMAIN,
    URL_GTFS_RT_TRIP_UPDATES,
    URL_GTFS_STATIC,
    URL_PLATFORM_BOARD,
)
from custom_components.sncf_opendata.diagnostics import (
    async_get_config_entry_diagnostics,
)
from tests.builders import A, RtTrip, StopTime, trip_id

from .conftest import ETAG, Feeds, paris


def entity_id(
    hass: HomeAssistant, entry: MockConfigEntry, domain: str, key: str
) -> str:
    registry = er.async_get(hass)
    subentry_id = next(iter(entry.subentries))
    found = registry.async_get_entity_id(domain, DOMAIN, f"{subentry_id}_{key}")
    assert found is not None
    return found


async def setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def tick(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, minutes: int
) -> None:
    freezer.tick(timedelta(minutes=minutes))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def test_setup_and_entities(
    hass: HomeAssistant,
    feeds: Feeds,
    at_16h: FrozenDateTimeFactory,
    make_entry: Callable[..., MockConfigEntry],
) -> None:
    entry = make_entry()
    await setup(hass, entry)
    assert entry.state is ConfigEntryState.LOADED

    sensor = hass.states.get(entity_id(hass, entry, "sensor", "next_departure"))
    assert sensor is not None
    # The next train that runs: the Mazamet one, on time.
    assert sensor.state == "2026-11-04T15:43:00+00:00"
    attrs = sensor.attributes
    assert attrs["train_number"] == "872410"
    assert attrs["status"] == "on_time"
    assert attrs["destinations"] == ["Saint-Sulpice"]
    assert attrs["realtime"] is True
    assert attrs["window_start"] == "2026-11-04T16:30:00+01:00"
    trains = {t["number"]: t for t in attrs["trains"]}
    assert set(trains) == {"872410", "870058", "870010"}
    assert trains["870058"]["delay"] == 8
    assert trains["870058"]["expected_departure"] == "2026-11-04T17:18:00+01:00"
    assert trains["870058"]["alerts"] == ["Train complet"]
    assert trains["870058"]["destinations"] == [
        "Saint-Sulpice",
        "Rabastens - Couffouleux",
    ]
    assert trains["870010"]["status"] == "cancelled"
    assert trains["870010"]["cancel_reason"] == "train_cancelled"

    disruption = hass.states.get(entity_id(hass, entry, "binary_sensor", "disruption"))
    assert disruption.state == STATE_ON
    assert disruption.attributes["cancelled"] == ["870010"]
    assert disruption.attributes["delayed"] == ["870058"]

    feed = hass.states.get("binary_sensor.sncf_open_data_realtime_feed")
    assert feed is not None
    assert feed.state == STATE_ON
    assert feed.attributes["timetable_version"] == "2026-11-03"

    event = hass.states.get(entity_id(hass, entry, "event", "train_update"))
    assert event.state == STATE_UNKNOWN

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_events(
    hass: HomeAssistant,
    feeds: Feeds,
    at_16h: FrozenDateTimeFactory,
    make_entry: Callable[..., MockConfigEntry],
) -> None:
    entry = make_entry()
    await setup(hass, entry)
    event_id = entity_id(hass, entry, "event", "train_update")
    fired: list[dict] = []

    def _record(event: Event[EventStateChangedData]) -> None:
        new = event.data["new_state"]
        if new is not None and new.state != STATE_UNKNOWN:
            fired.append(dict(new.attributes))

    async_track_state_change_event(hass, [event_id], _record)

    coordinator = entry.runtime_data.coordinator

    # First update after start: the silent baseline.
    at_16h.tick(timedelta(minutes=2))
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert fired == []

    feeds.rt_timestamp = paris("2026-11-04 16:04")
    feeds.rt_trips = [
        RtTrip(trip_id("872410"), stops=[StopTime(A, skipped=True)]),
        RtTrip(
            trip_id("870058"),
            stops=[
                StopTime(A, departure=paris("2026-11-04 17:25"), departure_delay=900)
            ],
        ),
    ]
    feeds.install()
    at_16h.tick(timedelta(minutes=2))
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert [(e["event_type"], e["number"]) for e in fired] == [
        ("cancelled", "872410"),
        ("delay_changed", "870058"),
    ]
    assert fired[1]["delay"] == 15
    assert fired[1]["previous_delay"] == 8
    assert fired[1]["route"] == "Retour"


async def test_realtime_outage(
    hass: HomeAssistant,
    feeds: Feeds,
    at_16h: FrozenDateTimeFactory,
    make_entry: Callable[..., MockConfigEntry],
    caplog: pytest.LogCaptureFixture,
) -> None:
    feeds.rt_status = 503
    feeds.install()
    entry = make_entry()
    await setup(hass, entry)
    feed = hass.states.get("binary_sensor.sncf_open_data_realtime_feed")
    assert feed.state == STATE_OFF
    assert "503" in feed.attributes["error"]
    sensor = hass.states.get(entity_id(hass, entry, "sensor", "next_departure"))
    assert sensor.attributes["realtime"] is False
    assert {t["status"] for t in sensor.attributes["trains"]} == {"scheduled"}
    # Still down: no second warning.
    await tick(hass, at_16h, 2)
    assert caplog.text.count("realtime feed unavailable") == 1
    # Back.
    feeds.rt_status = 200
    feeds.rt_timestamp = paris("2026-11-04 16:04")
    feeds.install()
    await tick(hass, at_16h, 2)
    assert (
        hass.states.get("binary_sensor.sncf_open_data_realtime_feed").state == STATE_ON
    )
    assert "available again" in caplog.text


async def test_stale_realtime_is_ignored(
    hass: HomeAssistant,
    feeds: Feeds,
    at_16h: FrozenDateTimeFactory,
    make_entry: Callable[..., MockConfigEntry],
) -> None:
    feeds.rt_timestamp = paris("2026-11-04 15:30")
    feeds.install()
    entry = make_entry()
    await setup(hass, entry)
    feed = hass.states.get("binary_sensor.sncf_open_data_realtime_feed")
    assert feed.state == STATE_OFF
    assert "out of date" in feed.attributes["error"]


async def test_idle_does_not_poll_realtime(
    hass: HomeAssistant,
    feeds: Feeds,
    freezer: FrozenDateTimeFactory,
    make_entry: Callable[..., MockConfigEntry],
) -> None:
    freezer.move_to(paris("2026-11-04 10:00"))
    entry = make_entry()
    await setup(hass, entry)
    assert feeds.calls(URL_GTFS_RT_TRIP_UPDATES) == []
    sensor = hass.states.get(entity_id(hass, entry, "sensor", "next_departure"))
    # The planned trains of the coming window are shown.
    assert sensor.attributes["realtime"] is False
    assert sensor.attributes["status"] == "scheduled"
    assert (
        hass.states.get("binary_sensor.sncf_open_data_realtime_feed").state
        == STATE_UNKNOWN
    )
    # Entering the hot period (one hour before the window) starts polling.
    freezer.move_to(paris("2026-11-04 15:35"))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert len(feeds.calls(URL_GTFS_RT_TRIP_UPDATES)) == 1


async def test_timetable_cache(
    hass: HomeAssistant,
    feeds: Feeds,
    at_16h: FrozenDateTimeFactory,
    make_entry: Callable[..., MockConfigEntry],
) -> None:
    entry = make_entry()
    await setup(hass, entry)
    assert len(feeds.calls(URL_GTFS_STATIC)) == 1
    assert os.path.exists(hass.config.cache_path(DOMAIN, "gtfs.zip"))

    # A reload within 12 hours reuses the stored index.
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert len(feeds.calls(URL_GTFS_STATIC)) == 1

    # The periodic refresh asks politely, and the server says "unchanged".
    feeds.static_status = 304
    feeds.install()
    await tick(hass, at_16h, 12 * 60)
    calls = feeds.calls(URL_GTFS_STATIC)
    assert len(calls) == 1
    assert calls[0][3]["If-None-Match"] == ETAG

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert not os.path.exists(hass.config.cache_path(DOMAIN, "gtfs.zip"))


async def test_setup_retry_without_timetable(
    hass: HomeAssistant,
    feeds: Feeds,
    at_16h: FrozenDateTimeFactory,
    make_entry: Callable[..., MockConfigEntry],
) -> None:
    feeds.static_exc = ClientError("boom")
    feeds.install()
    entry = make_entry()
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_corrupted_timetable(
    hass: HomeAssistant,
    feeds: Feeds,
    at_16h: FrozenDateTimeFactory,
    make_entry: Callable[..., MockConfigEntry],
) -> None:
    feeds.gtfs = b"not a zip"
    feeds.install()
    entry = make_entry()
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY
    # The bad archive is not kept.
    assert not os.path.exists(hass.config.cache_path(DOMAIN, "gtfs.zip"))


async def test_platforms(
    hass: HomeAssistant,
    feeds: Feeds,
    at_16h: FrozenDateTimeFactory,
    make_entry: Callable[..., MockConfigEntry],
) -> None:
    feeds.board = [
        {
            "trainNumber": "872410",
            "scheduledTime": "2026-11-04T15:43:00+00:00",
            "platform": {"track": "1C"},
        }
    ]
    feeds.install()
    entry = make_entry(platforms=True)
    await setup(hass, entry)
    assert feeds.calls(URL_PLATFORM_BOARD.format(uic10=A.zfill(10)))
    sensor = hass.states.get(entity_id(hass, entry, "sensor", "next_departure"))
    assert sensor.attributes["platform"] == "1C"
    feed = hass.states.get("binary_sensor.sncf_open_data_realtime_feed")
    assert feed.attributes["platforms_available"] is True


async def test_diagnostics(
    hass: HomeAssistant,
    feeds: Feeds,
    at_16h: FrozenDateTimeFactory,
    make_entry: Callable[..., MockConfigEntry],
) -> None:
    entry = make_entry()
    await setup(hass, entry)
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["timetable"]["feed_version"] == "2026-11-03"
    assert diagnostics["realtime"]["available"] is True
    assert diagnostics["realtime"]["update_interval"] == 120
    (route,) = diagnostics["routes"].values()
    assert route["weekdays"] == [0, 1, 2, 3, 4]
    (snapshot,) = diagnostics["snapshots"].values()
    assert len(snapshot["departures"]) == 3
