"""Regression test on real SNCF open data with replacement coaches.

The fixture is an extract of the real feeds of 5 November 2026 at 12:21, a
national strike day, cut by ``tests/fixtures/make_real_fixtures.py``. Many
trains between Toulouse and Rabastens were cancelled and replaced by coaches,
some of them only in the published timetable, some also in the realtime feed.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytest

from custom_components.sncf_opendata.const import TZ_PARIS
from custom_components.sncf_opendata.gtfs_realtime import (
    Alerts,
    TripUpdates,
    parse_alerts,
    parse_trip_updates,
)
from custom_components.sncf_opendata.gtfs_static import StaticIndex, parse_gtfs
from custom_components.sncf_opendata.timetable import (
    RouteConfig,
    RouteSnapshot,
    TrainStatus,
    build_snapshot,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "real_2026_11_05"
MATABIAU, SAINT_SULPICE, RABASTENS = "87611004", "87615344", "87615328"
NOW = datetime(2026, 11, 5, 12, 0, tzinfo=TZ_PARIS)
STRIKE = "Mouvement social national interprofessionnel"


@pytest.fixture(name="index", scope="module")
def fixture_index() -> StaticIndex:
    return parse_gtfs(
        FIXTURES / "gtfs.zip",
        {MATABIAU, SAINT_SULPICE, RABASTENS},
        date(2026, 11, 4),
        date(2026, 11, 13),
    )


@pytest.fixture(name="feeds", scope="module")
def fixture_feeds() -> tuple[TripUpdates, Alerts]:
    return (
        parse_trip_updates((FIXTURES / "trip_updates.pb").read_bytes()),
        parse_alerts((FIXTURES / "alerts.pb").read_bytes(), "fr"),
    )


def snapshot_of(
    index: StaticIndex,
    feeds: tuple[TripUpdates, Alerts],
    start: time,
    end: time,
    include_coaches: bool,
) -> RouteSnapshot:
    route = RouteConfig(
        route_id="r",
        name="Retour",
        origin=MATABIAU,
        destinations=(RABASTENS, SAINT_SULPICE),
        time_start=start,
        time_end=end,
        weekdays=frozenset(range(5)),
        delay_threshold=3,
        max_trains=20,
        include_coaches=include_coaches,
    )
    return build_snapshot(
        route, index, feeds[0], feeds[1], None, NOW, timedelta(minutes=2)
    )


def summary(snapshot: RouteSnapshot) -> list[tuple[str, str, str, TrainStatus]]:
    return [
        (
            d.scheduled_departure.astimezone(TZ_PARIS).strftime("%H:%M"),
            d.number,
            d.mode,
            d.status,
        )
        for d in snapshot.departures
    ]


def test_midday_with_realtime(
    index: StaticIndex, feeds: tuple[TripUpdates, Alerts]
) -> None:
    """Trains cancelled, coaches running in their place."""
    window = (time(12, 0), time(14, 0))
    trains_only = snapshot_of(index, feeds, *window, include_coaches=False)
    assert summary(trains_only) == [
        ("12:31", "870052", "TER", TrainStatus.CANCELLED),
        ("13:03", "871206", "TER", TrainStatus.CANCELLED),
        ("13:07", "870054", "TER", TrainStatus.CANCELLED),
        ("14:00", "872492", "TER", TrainStatus.SCHEDULED),
    ]
    with_coaches = snapshot_of(index, feeds, *window, include_coaches=True)
    assert summary(with_coaches) == [
        ("12:30", "462010", "Car TER", TrainStatus.ON_TIME),
        ("12:31", "870052", "TER", TrainStatus.CANCELLED),
        ("13:00", "462206", "Car TER", TrainStatus.ON_TIME),
        ("13:03", "871206", "TER", TrainStatus.CANCELLED),
        ("13:07", "870054", "TER", TrainStatus.CANCELLED),
        ("13:50", "462110", "Car TER", TrainStatus.SCHEDULED),
        ("14:00", "872492", "TER", TrainStatus.SCHEDULED),
    ]
    coach = with_coaches.departures[0]
    assert coach.realtime
    assert coach.destinations == [SAINT_SULPICE, RABASTENS]
    assert with_coaches.station_names[coach.terminus_uic] == "Albi Ville"
    assert STRIKE in coach.alerts
    assert with_coaches.next_departure(NOW) is coach


def test_evening_strike_plan(
    index: StaticIndex, feeds: tuple[TripUpdates, Alerts]
) -> None:
    """The evening coaches are only in the published timetable."""
    window = (time(16, 30), time(22, 0))
    trains_only = snapshot_of(index, feeds, *window, include_coaches=False)
    assert [n for _, n, _, _ in summary(trains_only)] == ["872412", "872496", "872416"]
    with_coaches = snapshot_of(index, feeds, *window, include_coaches=True)
    assert summary(with_coaches) == [
        ("17:19", "872412", "TER", TrainStatus.SCHEDULED),
        ("18:15", "872496", "TER", TrainStatus.SCHEDULED),
        ("18:47", "872416", "TER", TrainStatus.SCHEDULED),
        ("19:09", "462222", "Car TER", TrainStatus.SCHEDULED),
        ("19:35", "462034", "Car TER", TrainStatus.SCHEDULED),
        ("19:35", "462036", "Car TER", TrainStatus.SCHEDULED),
        ("21:25", "462038", "Car TER", TrainStatus.SCHEDULED),
    ]
    by_number = {d.number: d for d in with_coaches.departures}
    # One coach to Rabastens, one to Saint-Sulpice, at the same time.
    assert by_number["462034"].destinations == [RABASTENS]
    assert by_number["462036"].destinations == [SAINT_SULPICE]
    assert by_number["462038"].destinations == [SAINT_SULPICE, RABASTENS]
    assert (
        by_number["462034"].scheduled_arrival.astimezone(TZ_PARIS).strftime("%H:%M")
        == "20:20"
    )
