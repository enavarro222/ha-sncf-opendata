"""Tests of the merge of the timetable with realtime data."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
import io

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
    TrainStatus,
    build_snapshot,
)
from tests.builders import (
    DAY,
    A,
    B,
    C,
    D,
    RtTrip,
    StopTime,
    make_alerts,
    make_gtfs_zip,
    make_trip_updates,
    trip_id,
)

GRACE = timedelta(minutes=2)


def at(hhmm: str, day: date = DAY) -> datetime:
    return datetime.combine(day, time.fromisoformat(hhmm), tzinfo=TZ_PARIS)


@pytest.fixture(name="index")
def fixture_index() -> StaticIndex:
    return parse_gtfs(
        io.BytesIO(make_gtfs_zip()), {A, B, C, D}, date(2026, 11, 3), date(2026, 11, 12)
    )


def route(
    destinations: tuple[str, ...] = (C, B),
    start: str = "16:30",
    end: str = "20:00",
    weekdays: frozenset[int] = frozenset(range(5)),
) -> RouteConfig:
    return RouteConfig(
        route_id="r1",
        name="Retour",
        origin=A,
        destinations=destinations,
        time_start=time.fromisoformat(start),
        time_end=time.fromisoformat(end),
        weekdays=weekdays,
        delay_threshold=3,
        max_trains=10,
    )


def updates_of(trips: list[RtTrip], now: datetime):
    return parse_trip_updates(make_trip_updates(trips, now))


def test_window() -> None:
    weekdays = route()
    assert weekdays.window(at("12:00")) == (at("16:30"), at("20:00"))
    # After the end, the next working day.
    assert weekdays.window(at("20:01")) == (
        at("16:30", DAY + timedelta(days=1)),
        at("20:00", DAY + timedelta(days=1)),
    )
    # Friday evening -> Monday.
    friday = date(2026, 11, 6)
    assert weekdays.window(at("21:00", friday))[0] == at("16:30", date(2026, 11, 9))
    assert route(weekdays=frozenset()).window(at("12:00")) is None


def test_without_realtime(index: StaticIndex) -> None:
    now = at("16:00")
    snapshot = build_snapshot(route(), index, None, None, None, now, GRACE)
    assert snapshot.window_start == at("16:30")
    assert not snapshot.realtime
    assert [(d.number, d.status) for d in snapshot.departures] == [
        ("872410", TrainStatus.SCHEDULED),
        ("870058", TrainStatus.SCHEDULED),
    ]
    albi = snapshot.departures[1]
    assert albi.destinations == [B, C]
    assert albi.destination_uic == B
    assert albi.as_dict(snapshot.station_names)["destinations"] == [
        "Saint-Sulpice",
        "Rabastens - Couffouleux",
    ]
    assert albi.as_dict(snapshot.station_names)["terminus"] == "Albi Ville"
    assert snapshot.next_departure(now).number == "872410"


def test_on_time_and_delayed(index: StaticIndex) -> None:
    now = at("16:00")
    updates = updates_of(
        [
            RtTrip(
                trip_id("872410"),
                stops=[StopTime(A, departure=at("16:43"), departure_delay=0)],
            ),
            RtTrip(
                trip_id("870058"),
                stops=[
                    StopTime(A, departure=at("17:18"), departure_delay=480),
                    StopTime(B, arrival=at("17:44"), arrival_delay=480),
                ],
            ),
        ],
        now,
    )
    snapshot = build_snapshot(route(), index, updates, None, None, now, GRACE)
    mazamet, albi = snapshot.departures
    assert mazamet.status is TrainStatus.ON_TIME
    assert mazamet.realtime
    assert albi.status is TrainStatus.DELAYED
    assert albi.delay == 8
    assert albi.expected_departure == at("17:18")
    assert albi.expected_arrival == at("17:44")
    assert snapshot.realtime_timestamp == updates.timestamp


def test_delay_propagates_from_earlier_stop(index: StaticIndex) -> None:
    """The origin has no update: the delay of an earlier call applies."""
    now = at("16:00")
    updates = updates_of(
        [
            RtTrip(
                trip_id("870058"),
                stops=[
                    # A stop before the origin (here a made-up one).
                    StopTime("87999999", departure=at("17:05"), departure_delay=600),
                    # After the origin: must not be used for the departure.
                    StopTime(D, arrival=at("18:51"), arrival_delay=1800),
                ],
            )
        ],
        now,
    )
    albi = build_snapshot(route(), index, updates, None, None, now, GRACE).departures[1]
    assert albi.delay == 10
    assert albi.status is TrainStatus.DELAYED


def test_trip_without_usable_update(index: StaticIndex) -> None:
    now = at("16:00")
    updates = updates_of(
        [
            RtTrip(
                trip_id("870058"),
                stops=[StopTime(D, arrival=at("18:51"), arrival_delay=1800)],
            )
        ],
        now,
    )
    albi = build_snapshot(route(), index, updates, None, None, now, GRACE).departures[1]
    assert albi.status is TrainStatus.SCHEDULED
    assert not albi.realtime


def test_cancellations(index: StaticIndex) -> None:
    now = at("16:00")
    snapshot = build_snapshot(
        route(),
        index,
        updates_of(
            [
                RtTrip("OCESN870058F", relationship="CANCELED"),
                RtTrip(trip_id("872410"), stops=[StopTime(A, skipped=True)]),
            ],
            now,
        ),
        None,
        None,
        now,
        GRACE,
    )
    mazamet, albi = snapshot.departures
    assert albi.cancel_reason is CancelReason.TRAIN_CANCELLED
    assert mazamet.cancel_reason is CancelReason.ORIGIN_SKIPPED
    assert albi.cancelled
    assert snapshot.next_departure(now) is None


def test_destination_skipped(index: StaticIndex) -> None:
    now = at("16:00")

    def albi_with(*stops: StopTime):
        updates = updates_of([RtTrip(trip_id("870058"), stops=list(stops))], now)
        return build_snapshot(
            route(), index, updates, None, None, now, GRACE
        ).departures[1]

    # Saint-Sulpice skipped, Rabastens still served.
    albi = albi_with(
        StopTime(A, departure=at("17:10"), departure_delay=0),
        StopTime(B, skipped=True),
    )
    assert albi.status is TrainStatus.ON_TIME
    assert albi.destinations == [C]
    assert albi.destination_uic == C
    # Both skipped.
    albi = albi_with(StopTime(B, skipped=True), StopTime(C, skipped=True))
    assert albi.cancel_reason is CancelReason.DESTINATION_SKIPPED


def test_realtime_only_trains(index: StaticIndex) -> None:
    """Cancelled trains removed from the timetable, and added trains."""
    now = at("16:00")
    updates = updates_of(
        [
            RtTrip(
                "OCESN870010F",
                relationship="CANCELED",
                stops=[
                    StopTime(
                        A,
                        departure=at("17:36"),
                        departure_delay=0,
                        skipped=True,
                        stop_area=True,
                    ),
                    StopTime(
                        B,
                        arrival=at("18:01"),
                        arrival_delay=0,
                        skipped=True,
                        stop_area=True,
                    ),
                    StopTime(
                        D,
                        arrival=at("19:00"),
                        arrival_delay=0,
                        skipped=True,
                        stop_area=True,
                    ),
                ],
            ),
            RtTrip(
                "OCESN871000F",
                relationship="ADDED",
                stops=[
                    StopTime(A, departure=at("18:05"), departure_delay=120),
                    StopTime(C, arrival=at("18:40"), arrival_delay=120),
                ],
            ),
            # Out of the window, wrong way, unknown origin: ignored.
            RtTrip(
                "OCESN871001F",
                relationship="ADDED",
                stops=[
                    StopTime(A, departure=at("21:00")),
                    StopTime(C, arrival=at("21:30")),
                ],
            ),
            RtTrip(
                "OCESN871002F",
                relationship="ADDED",
                stops=[
                    StopTime(C, departure=at("17:00")),
                    StopTime(A, arrival=at("17:30")),
                ],
            ),
            RtTrip(
                "OCESN871003F",
                relationship="ADDED",
                stops=[
                    StopTime(A, departure=at("17:00")),
                    StopTime(D, arrival=at("17:30")),
                ],
            ),
        ],
        now,
    )
    snapshot = build_snapshot(route(), index, updates, None, None, now, GRACE)
    by_number = {d.number: d for d in snapshot.departures}
    assert set(by_number) == {"872410", "870058", "870010", "871000"}
    cancelled = by_number["870010"]
    assert cancelled.cancelled
    assert cancelled.scheduled_departure == at("17:36")
    assert cancelled.destinations == [B]
    assert cancelled.terminus_uic == D
    assert cancelled.mode == ""
    added = by_number["871000"]
    assert added.added
    assert added.scheduled_departure == at("18:03")
    assert added.delay == 2
    assert added.expected_arrival == at("18:40")
    assert added.status is TrainStatus.DELAYED


def test_alerts_platforms_and_departed(index: StaticIndex) -> None:
    now = at("17:13")
    alerts = parse_alerts(
        make_alerts(
            [
                ("1", ["OCESN870058F"], {"fr": "Train complet"}, []),
                ("2", ["OCESN870058F"], {"fr": "Train complet"}, []),
            ],
            now,
        )
    )
    platforms = {("870058", "17:10"): "1C"}
    updates = updates_of(
        [
            RtTrip(
                trip_id("870058"),
                stops=[StopTime(A, departure=at("17:20"), departure_delay=600)],
            )
        ],
        now,
    )
    snapshot = build_snapshot(route(), index, updates, alerts, platforms, now, GRACE)
    # The Mazamet train left at 16:43; the late Albi train is still there.
    assert [d.number for d in snapshot.departures] == ["870058"]
    albi = snapshot.departures[0]
    assert albi.alerts == ["Train complet"]
    assert albi.platform == "1C"
    assert snapshot.upcoming(now) == [albi]
    assert snapshot.upcoming(at("17:30")) == []


def test_no_window() -> None:
    index = parse_gtfs(io.BytesIO(make_gtfs_zip()), {A, C}, DAY, DAY)
    snapshot = build_snapshot(
        route(weekdays=frozenset()), index, None, None, None, at("12:00"), GRACE
    )
    assert snapshot.window_start is None
    assert snapshot.departures == []
