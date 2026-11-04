"""Regression test on real SNCF open data (4 November 2026, evening).

The fixture is an extract of the real feeds of that evening, cut by
``tests/fixtures/make_real_fixtures.py``: the trains between Toulouse
Matabiau, Saint-Sulpice and Rabastens. That day a national strike was on,
and SNCF had removed train 870010 (17:36) from the published timetable while
the realtime feed still listed it as cancelled.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytest

from custom_components.sncf_opendata.const import TZ_PARIS
from custom_components.sncf_opendata.gtfs_realtime import (
    parse_alerts,
    parse_trip_updates,
)
from custom_components.sncf_opendata.gtfs_static import StaticIndex, parse_gtfs
from custom_components.sncf_opendata.timetable import (
    CancelReason,
    RouteConfig,
    RouteSnapshot,
    TrainStatus,
    build_snapshot,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "real"
MATABIAU, SAINT_SULPICE, RABASTENS = "87611004", "87615344", "87615328"
NOW = datetime(2026, 11, 4, 16, 0, tzinfo=TZ_PARIS)


@pytest.fixture(name="index", scope="module")
def fixture_index() -> StaticIndex:
    return parse_gtfs(
        FIXTURES / "gtfs.zip",
        {MATABIAU, SAINT_SULPICE, RABASTENS},
        date(2026, 11, 3),
        date(2026, 11, 12),
    )


@pytest.fixture(name="snapshot", scope="module")
def fixture_snapshot(index: StaticIndex) -> RouteSnapshot:
    route = RouteConfig(
        route_id="r",
        name="Retour",
        origin=MATABIAU,
        destinations=(RABASTENS, SAINT_SULPICE),
        time_start=time(16, 30),
        time_end=time(20, 0),
        weekdays=frozenset(range(5)),
        delay_threshold=3,
        max_trains=20,
    )
    updates = parse_trip_updates((FIXTURES / "trip_updates.pb").read_bytes())
    alerts = parse_alerts((FIXTURES / "alerts.pb").read_bytes(), "fr")
    return build_snapshot(
        route, index, updates, alerts, None, NOW, timedelta(minutes=2)
    )


def local(value: datetime) -> str:
    return value.astimezone(TZ_PARIS).strftime("%H:%M")


def test_timetable(index: StaticIndex) -> None:
    assert index.feed_version == "2026-11-03"
    assert [s.name for s in index.search_stations("st sulpice")] == ["Saint-Sulpice"]
    assert index.station_name(MATABIAU) == "Toulouse Matabiau"


def test_departures_of_the_evening(snapshot: RouteSnapshot) -> None:
    assert [(local(d.scheduled_departure), d.number) for d in snapshot.departures] == [
        ("16:35", "870056"),
        ("16:43", "872410"),
        ("17:01", "871208"),
        ("17:10", "870058"),
        ("17:19", "872412"),
        ("17:36", "870010"),
        ("17:40", "872414"),
        ("17:52", "870060"),
        ("18:11", "871210"),
        ("18:15", "870062"),
        ("18:38", "870012"),
        ("18:47", "872416"),
        ("19:09", "871212"),
        ("19:16", "870064"),
        ("19:29", "870014"),
    ]
    assert all(d.mode == "TER" for d in snapshot.departures)


def test_strike_cancellation(snapshot: RouteSnapshot) -> None:
    """870010 is only in the realtime feed, and must show as cancelled."""
    train = next(d for d in snapshot.departures if d.number == "870010")
    assert train.status is TrainStatus.CANCELLED
    assert train.cancel_reason is CancelReason.TRAIN_CANCELLED
    assert train.realtime
    assert train.destinations == [SAINT_SULPICE, RABASTENS]
    assert snapshot.station_names[train.terminus_uic] == "Rodez"
    assert "Mouvement social national interprofessionnel" in train.alerts


def test_destinations_served(snapshot: RouteSnapshot) -> None:
    """Albi line trains serve both stations, Mazamet and Figeac ones only one."""
    by_number = {d.number: d for d in snapshot.departures}
    assert by_number["870058"].destinations == [SAINT_SULPICE, RABASTENS]
    assert snapshot.station_names[by_number["870058"].terminus_uic] == "Albi Ville"
    assert by_number["872410"].destinations == [SAINT_SULPICE]
    assert snapshot.station_names[by_number["872410"].terminus_uic] == "Mazamet"
    assert by_number["871208"].destinations == [SAINT_SULPICE]


def test_realtime_status(snapshot: RouteSnapshot) -> None:
    """The snapshot of the feed was taken at 19:46: earlier trains had left it."""
    by_number = {d.number: d for d in snapshot.departures}
    for number in ("871210", "870012", "872416", "871212", "870064", "870014"):
        assert by_number[number].status is TrainStatus.ON_TIME
        assert by_number[number].realtime
    assert by_number["870058"].status is TrainStatus.SCHEDULED
    assert not by_number["870058"].realtime
    assert snapshot.next_departure(NOW).number == "870056"
