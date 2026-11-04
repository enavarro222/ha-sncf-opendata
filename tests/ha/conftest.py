"""Fixtures of the Home Assistant tests."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigSubentryData
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from custom_components.sncf_opendata.const import (
    CONF_DELAY_THRESHOLD,
    CONF_DESTINATIONS,
    CONF_MAX_TRAINS,
    CONF_ORIGIN,
    CONF_PLATFORMS,
    CONF_REALTIME_INTERVAL,
    CONF_TIME_END,
    CONF_TIME_START,
    CONF_WEEKDAYS,
    DOMAIN,
    SUBENTRY_TYPE_ROUTE,
    TZ_PARIS,
    URL_GTFS_RT_SERVICE_ALERTS,
    URL_GTFS_RT_TRIP_UPDATES,
    URL_GTFS_STATIC,
    URL_PLATFORM_BOARD,
)
from tests.builders import (
    A,
    B,
    C,
    RtTrip,
    StopTime,
    make_alerts,
    make_gtfs_zip,
    make_trip_updates,
    trip_id,
)

ETAG = '"v1"'


def paris(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=TZ_PARIS)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load the integration from custom_components."""


@pytest.fixture(autouse=True)
async def paris_time_zone(hass: HomeAssistant) -> None:
    await hass.config.async_set_time_zone("Europe/Paris")


def default_rt_trips() -> list[RtTrip]:
    return [
        RtTrip(
            trip_id("872410"),
            stops=[StopTime(A, departure=paris("2026-11-04 16:43"), departure_delay=0)],
        ),
        RtTrip(
            trip_id("870058"),
            stops=[
                StopTime(A, departure=paris("2026-11-04 17:18"), departure_delay=480)
            ],
        ),
        RtTrip(
            "OCESN870010F",
            relationship="CANCELED",
            stops=[
                StopTime(
                    A, departure=paris("2026-11-04 17:36"), skipped=True, stop_area=True
                ),
                StopTime(
                    B, arrival=paris("2026-11-04 18:01"), skipped=True, stop_area=True
                ),
            ],
        ),
    ]


@dataclass
class Feeds:
    """The fake SNCF servers."""

    mock: AiohttpClientMocker
    gtfs: bytes = field(default_factory=make_gtfs_zip)
    rt_trips: list[RtTrip] = field(default_factory=default_rt_trips)
    rt_timestamp: datetime = field(default_factory=lambda: paris("2026-11-04 16:00"))
    alerts: list[Any] = field(
        default_factory=lambda: [("1", ["OCESN870058F"], {"fr": "Train complet"}, [])]
    )
    static_status: int = 200
    static_exc: Exception | None = None
    rt_status: int = 200
    rt_content: bytes | None = None
    board: Any = None

    def install(self) -> None:
        """(Re)register the responses."""
        self.mock.clear_requests()
        if self.static_exc is not None:
            self.mock.get(URL_GTFS_STATIC, exc=self.static_exc)
        else:
            self.mock.get(
                URL_GTFS_STATIC,
                status=self.static_status,
                content=self.gtfs if self.static_status == 200 else b"",
                headers={
                    "ETag": ETAG,
                    "Last-Modified": "Tue, 03 Nov 2026 20:52:38 GMT",
                },
            )
        self.mock.get(
            URL_GTFS_RT_TRIP_UPDATES,
            status=self.rt_status,
            content=self.rt_content
            if self.rt_content is not None
            else make_trip_updates(self.rt_trips, self.rt_timestamp),
        )
        self.mock.get(
            URL_GTFS_RT_SERVICE_ALERTS,
            content=make_alerts(self.alerts, self.rt_timestamp),
        )
        if self.board is not None:
            self.mock.get(URL_PLATFORM_BOARD.format(uic10=A.zfill(10)), json=self.board)

    def calls(self, url: str) -> list[tuple[Any, ...]]:
        return [call for call in self.mock.mock_calls if str(call[1]) == url]


@pytest.fixture
def feeds(aioclient_mock: AiohttpClientMocker) -> Feeds:
    feeds = Feeds(aioclient_mock)
    feeds.install()
    return feeds


def route_data(**overrides: Any) -> dict[str, Any]:
    data = {
        CONF_ORIGIN: A,
        CONF_DESTINATIONS: [C, B],
        CONF_TIME_START: "16:30:00",
        CONF_TIME_END: "20:00:00",
        CONF_WEEKDAYS: ["mon", "tue", "wed", "thu", "fri"],
        CONF_DELAY_THRESHOLD: 3,
        CONF_MAX_TRAINS: 6,
    }
    return data | overrides


@pytest.fixture
def make_entry(hass: HomeAssistant) -> Callable[..., MockConfigEntry]:
    def _make(platforms: bool = False, routes: int = 1) -> MockConfigEntry:
        entry = MockConfigEntry(
            domain=DOMAIN,
            title="SNCF Open Data",
            data={},
            options={CONF_REALTIME_INTERVAL: 120, CONF_PLATFORMS: platforms},
            subentries_data=[
                ConfigSubentryData(
                    data=route_data(),
                    subentry_type=SUBENTRY_TYPE_ROUTE,
                    title="Retour",
                    unique_id="route-1",
                )
            ][:routes],
        )
        entry.add_to_hass(hass)
        return entry

    return _make


@pytest.fixture
def at_16h(freezer: FrozenDateTimeFactory) -> FrozenDateTimeFactory:
    """A Wednesday, half an hour before the window opens."""
    freezer.move_to(paris("2026-11-04 16:00"))
    return freezer
