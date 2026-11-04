"""Tests of the change detection."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, time, timedelta

from custom_components.sncf_opendata.changes import ChangeTracker, ChangeType
from custom_components.sncf_opendata.const import TZ_PARIS
from custom_components.sncf_opendata.timetable import (
    Departure,
    RouteConfig,
    RouteSnapshot,
    TrainStatus,
)

DAY = date(2026, 11, 4)
ROUTE = RouteConfig(
    "r", "Retour", "A", ("C",), time(16, 30), time(20), frozenset(range(7)), 3, 10
)


def at(hhmm: str) -> datetime:
    return datetime.combine(DAY, time.fromisoformat(hhmm), tzinfo=TZ_PARIS)


def train(
    number: str = "870058",
    delay: int = 0,
    status: TrainStatus | None = None,
    platform: str | None = None,
    realtime: bool = True,
    departure: str = "17:10",
) -> Departure:
    scheduled = at(departure)
    if status is None:
        status = (
            TrainStatus.SCHEDULED
            if not realtime
            else TrainStatus.DELAYED
            if delay
            else TrainStatus.ON_TIME
        )
    return Departure(
        number=number,
        mode="TER",
        service_date=DAY,
        origin_uic="A",
        destination_uic="C",
        terminus_uic="D",
        scheduled_departure=scheduled,
        expected_departure=scheduled + timedelta(minutes=delay),
        scheduled_arrival=scheduled + timedelta(minutes=30),
        expected_arrival=scheduled + timedelta(minutes=30 + delay),
        status=status,
        realtime=realtime,
        platform=platform,
    )


def snap(*departures: Departure, realtime: bool = True) -> RouteSnapshot:
    return RouteSnapshot(
        ROUTE, at("16:30"), at("20:00"), list(departures), realtime, None, {}
    )


def changes(tracker: ChangeTracker, snapshot: RouteSnapshot, now: str = "16:00"):
    return [
        (c.change, c.departure.number, c.previous_delay)
        for c in tracker.update(snapshot, at(now))
    ]


def test_baseline_is_silent_with_realtime() -> None:
    tracker = ChangeTracker()
    assert (
        changes(
            tracker, snap(train(delay=10), train("2", status=TrainStatus.CANCELLED))
        )
        == []
    )
    # Nothing new: still silent.
    assert (
        changes(
            tracker, snap(train(delay=10), train("2", status=TrainStatus.CANCELLED))
        )
        == []
    )


def test_first_realtime_after_idle_start_is_reported() -> None:
    """Started outside the window: the first realtime data is news."""
    tracker = ChangeTracker()
    assert changes(tracker, snap(train(realtime=False), realtime=False)) == []
    assert changes(tracker, snap(train(delay=5))) == [(ChangeType.DELAYED, "870058", 0)]


def test_delay_sequence() -> None:
    tracker = ChangeTracker()
    changes(tracker, snap(train()))
    assert changes(tracker, snap(train(delay=2))) == []
    assert changes(tracker, snap(train(delay=3))) == [(ChangeType.DELAYED, "870058", 0)]
    # Growing a minute at a time is reported once it moved by the threshold.
    assert changes(tracker, snap(train(delay=4))) == []
    assert changes(tracker, snap(train(delay=5))) == []
    assert changes(tracker, snap(train(delay=6))) == [
        (ChangeType.DELAY_CHANGED, "870058", 3)
    ]
    assert changes(tracker, snap(train(delay=1))) == [
        (ChangeType.BACK_ON_TIME, "870058", 6)
    ]
    assert changes(tracker, snap(train(delay=0))) == []


def test_feed_outage_is_not_back_on_time() -> None:
    tracker = ChangeTracker()
    changes(tracker, snap(train(delay=10)))
    assert changes(tracker, snap(train(realtime=False), realtime=False)) == []
    assert changes(tracker, snap(train(delay=10))) == []


def test_cancelled_once() -> None:
    tracker = ChangeTracker()
    changes(tracker, snap(train()))
    cancelled = train(status=TrainStatus.CANCELLED)
    assert changes(tracker, snap(cancelled)) == [(ChangeType.CANCELLED, "870058", None)]
    assert changes(tracker, snap(cancelled)) == []
    # A train appearing already cancelled (removed from the timetable).
    assert changes(
        tracker, snap(cancelled, train("870010", status=TrainStatus.CANCELLED))
    ) == [(ChangeType.CANCELLED, "870010", None)]


def test_platforms() -> None:
    tracker = ChangeTracker()
    changes(tracker, snap(train()))
    result = tracker.update(snap(train(platform="1C")), at("16:50"))
    assert [(c.change, c.previous_platform) for c in result] == [
        (ChangeType.PLATFORM_ASSIGNED, None)
    ]
    # Unknown again (source down): nothing.
    assert changes(tracker, snap(train())) == []
    result = tracker.update(snap(train(platform="2")), at("16:55"))
    assert [(c.change, c.previous_platform) for c in result] == [
        (ChangeType.PLATFORM_CHANGED, "1C")
    ]
    # Platforms work without realtime delays too.
    result = tracker.update(
        snap(train(platform="3", realtime=False), realtime=False), at("16:56")
    )
    assert [c.change for c in result] == [ChangeType.PLATFORM_CHANGED]


def test_departed_and_forgotten_trains() -> None:
    tracker = ChangeTracker()
    changes(tracker, snap(train()))
    # Departed: a late update is not reported.
    assert changes(tracker, snap(train(delay=5)), now="17:30") == []
    # Gone from the snapshot, then back: starts from scratch.
    changes(tracker, snap())
    assert changes(tracker, snap(train(delay=5))) == [(ChangeType.DELAYED, "870058", 0)]


def test_new_train_with_platform_and_delay() -> None:
    tracker = ChangeTracker()
    changes(tracker, snap())
    result = tracker.update(snap(replace(train(delay=4), platform="B")), at("16:00"))
    assert [c.change for c in result] == [
        ChangeType.DELAYED,
        ChangeType.PLATFORM_ASSIGNED,
    ]
