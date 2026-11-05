"""Departures of a route: the timetable merged with realtime data.

This module has no Home Assistant dependency.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from typing import Any

from .const import TZ_PARIS
from .gtfs_realtime import (
    Alerts,
    RealtimeTrip,
    StopUpdate,
    TripRelationship,
    TripUpdates,
)
from .gtfs_static import ScheduledDeparture, StaticIndex


class TrainStatus(StrEnum):
    """Status of a departure."""

    ON_TIME = "on_time"
    DELAYED = "delayed"
    CANCELLED = "cancelled"
    # No realtime information: the planned time is all we know.
    SCHEDULED = "scheduled"


class CancelReason(StrEnum):
    """Why a departure does not take place for this route."""

    TRAIN_CANCELLED = "train_cancelled"
    ORIGIN_SKIPPED = "origin_skipped"
    DESTINATION_SKIPPED = "destination_skipped"


@dataclass(frozen=True, slots=True)
class RouteConfig:
    """What a route follows."""

    route_id: str
    name: str
    origin: str
    destinations: tuple[str, ...]
    time_start: time
    time_end: time
    weekdays: frozenset[int]  # 0 = Monday
    delay_threshold: int  # minutes
    max_trains: int
    # Also follow road services: replacement coaches, regional coaches.
    include_coaches: bool = False

    def window(self, now: datetime) -> tuple[datetime, datetime] | None:
        """Return the current window, or the next one if today's is over."""
        local_now = now.astimezone(TZ_PARIS)
        for offset in range(8):
            day = local_now.date() + timedelta(days=offset)
            if day.weekday() not in self.weekdays:
                continue
            start = datetime.combine(day, self.time_start, tzinfo=TZ_PARIS)
            end = datetime.combine(day, self.time_end, tzinfo=TZ_PARIS)
            if local_now <= end:
                # UTC, so that later arithmetic is in real time.
                return start.astimezone(UTC), end.astimezone(UTC)
        return None


@dataclass(slots=True)
class Departure:
    """A train of a route, with what is known about it now."""

    number: str
    mode: str
    service_date: date
    origin_uic: str
    destination_uic: str
    terminus_uic: str | None
    scheduled_departure: datetime
    expected_departure: datetime
    scheduled_arrival: datetime
    expected_arrival: datetime
    status: TrainStatus
    realtime: bool = False
    added: bool = False
    cancel_reason: CancelReason | None = None
    platform: str | None = None
    alerts: list[str] = field(default_factory=list)
    # Every configured destination this train serves, in calling order.
    destinations: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        """Identify the train across updates."""
        return f"{self.number}_{self.service_date.isoformat()}"

    @property
    def delay(self) -> int:
        """Return the departure delay in whole minutes (never negative)."""
        seconds = (self.expected_departure - self.scheduled_departure).total_seconds()
        return max(0, int(seconds // 60))

    @property
    def cancelled(self) -> bool:
        """Tell whether the train does not run for this route."""
        return self.status is TrainStatus.CANCELLED

    def as_dict(self, names: Mapping[str, str]) -> dict[str, Any]:
        """Serialize for entity attributes and event data."""
        return {
            "number": self.number,
            "mode": self.mode,
            "service_date": self.service_date.isoformat(),
            "origin": names.get(self.origin_uic, self.origin_uic),
            "destination": names.get(self.destination_uic, self.destination_uic),
            "destinations": [names.get(uic, uic) for uic in self.destinations],
            "terminus": names.get(self.terminus_uic, self.terminus_uic)
            if self.terminus_uic
            else None,
            "scheduled_departure": local_iso(self.scheduled_departure),
            "expected_departure": local_iso(self.expected_departure),
            "scheduled_arrival": local_iso(self.scheduled_arrival),
            "expected_arrival": local_iso(self.expected_arrival),
            "delay": self.delay,
            "status": self.status.value,
            "realtime": self.realtime,
            "added": self.added,
            "cancel_reason": self.cancel_reason.value if self.cancel_reason else None,
            "platform": self.platform,
            "alerts": list(self.alerts),
        }


def _ts(value: int) -> datetime:
    return datetime.fromtimestamp(value, tz=UTC)


def local_iso(value: datetime) -> str:
    """Format an instant in French local time."""
    return value.astimezone(TZ_PARIS).isoformat()


def _expected(
    trip: RealtimeTrip, uic: str, scheduled: datetime, departure: bool
) -> datetime | None:
    """Return the expected time of a call from the realtime trip.

    When the call itself has no update, the delay of the last updated call
    planned before it applies, as the GTFS-RT specification says.
    """
    update = trip.stop(uic)
    if update is not None:
        return _time_of(update, scheduled, departure)
    planned = int(scheduled.timestamp())
    earlier = [
        (stamp, stop)
        for stop in trip.stops
        if not stop.skipped
        and (stamp := stop.scheduled_time()) is not None
        and stamp <= planned
    ]
    if not earlier:
        return None
    _, previous = max(earlier, key=lambda item: item[0])
    delay = previous.departure_delay
    if delay is None:
        delay = previous.arrival_delay
    if delay is None:
        return None
    return scheduled + timedelta(seconds=delay)


def _time_of(
    update: StopUpdate, scheduled: datetime, departure: bool
) -> datetime | None:
    pairs = (
        (
            (update.departure_time, update.departure_delay),
            (update.arrival_time, update.arrival_delay),
        )
        if departure
        else (
            (update.arrival_time, update.arrival_delay),
            (update.departure_time, update.departure_delay),
        )
    )
    for stamp, delay in pairs:
        if stamp is not None:
            return _ts(stamp)
        if delay is not None:
            return scheduled + timedelta(seconds=delay)
    return None


def _status(departure: datetime, scheduled: datetime) -> TrainStatus:
    return (
        TrainStatus.DELAYED
        if (departure - scheduled).total_seconds() >= 60
        else TrainStatus.ON_TIME
    )


def merge_departure(
    scheduled: ScheduledDeparture, updates: TripUpdates | None
) -> Departure:
    """Apply the realtime feed to a planned departure."""
    departure = Departure(
        number=scheduled.number,
        mode=scheduled.mode,
        service_date=scheduled.service_date,
        origin_uic=scheduled.origin_uic,
        destination_uic=scheduled.destination_uic,
        terminus_uic=scheduled.terminus_uic,
        scheduled_departure=scheduled.scheduled_departure,
        expected_departure=scheduled.scheduled_departure,
        scheduled_arrival=scheduled.scheduled_arrival,
        expected_arrival=scheduled.scheduled_arrival,
        status=TrainStatus.SCHEDULED,
        destinations=[uic for uic, _ in scheduled.destinations],
    )
    if updates is None:
        return departure
    trip = updates.find(scheduled.trip_id, scheduled.number, scheduled.service_date)
    if trip is None:
        return departure
    departure.realtime = True
    if trip.relationship is TripRelationship.CANCELED:
        departure.status = TrainStatus.CANCELLED
        departure.cancel_reason = CancelReason.TRAIN_CANCELLED
        return departure
    origin_update = trip.stop(scheduled.origin_uic)
    if origin_update is not None and origin_update.skipped:
        departure.status = TrainStatus.CANCELLED
        departure.cancel_reason = CancelReason.ORIGIN_SKIPPED
        return departure
    served = [
        (uic, arrival)
        for uic, arrival in scheduled.destinations
        if not ((stop := trip.stop(uic)) is not None and stop.skipped)
    ]
    if not served:
        departure.status = TrainStatus.CANCELLED
        departure.cancel_reason = CancelReason.DESTINATION_SKIPPED
        return departure
    destination, arrival = served[0]
    departure.destinations = [uic for uic, _ in served]
    departure.destination_uic = destination
    departure.scheduled_arrival = arrival
    departure.expected_arrival = arrival
    expected = _expected(
        trip, scheduled.origin_uic, scheduled.scheduled_departure, True
    )
    if expected is None:
        # The trip is in the feed but says nothing usable about our station.
        departure.realtime = False
        return departure
    departure.expected_departure = expected
    expected_arrival = _expected(trip, destination, arrival, False)
    if expected_arrival is not None:
        departure.expected_arrival = expected_arrival
    departure.status = _status(expected, scheduled.scheduled_departure)
    return departure


def realtime_only_departures(
    route: RouteConfig,
    updates: TripUpdates,
    start: datetime,
    end: datetime,
    known: set[str],
    static: StaticIndex | None = None,
) -> list[Departure]:
    """Return the trains of a route that only the realtime feed knows.

    These are trains added to the timetable, and cancelled trains that SNCF
    already removed from the published timetable (strikes, works): the
    realtime feed still lists them as cancelled, and travellers expect them.
    """
    cancelled = [
        trip
        for trip in updates.by_number.values()
        if trip.relationship is TripRelationship.CANCELED
    ]
    result = []
    for trip in [*updates.added, *cancelled]:
        is_cancelled = trip.relationship is TripRelationship.CANCELED
        if not trip.number or trip.number in known:
            continue
        if trip.road and not route.include_coaches:
            continue
        origin_pos = next(
            (
                i
                for i, s in enumerate(trip.stops)
                if s.uic == route.origin and (is_cancelled or not s.skipped)
            ),
            None,
        )
        if origin_pos is None:
            continue
        served = [
            s
            for s in trip.stops[origin_pos + 1 :]
            if s.uic in route.destinations and (is_cancelled or not s.skipped)
        ]
        destination = served[0] if served else None
        origin = trip.stops[origin_pos]
        planned_dep = origin.scheduled_time()
        if destination is None or planned_dep is None:
            continue
        scheduled_departure = _ts(planned_dep)
        if not start <= scheduled_departure <= end:
            continue
        planned_arr = destination.scheduled_time()
        scheduled_arrival = _ts(planned_arr) if planned_arr else scheduled_departure
        if is_cancelled:
            expected_departure, expected_arrival = (
                scheduled_departure,
                scheduled_arrival,
            )
            status = TrainStatus.CANCELLED
        else:
            expected_departure = (
                _time_of(origin, scheduled_departure, True) or scheduled_departure
            )
            expected_arrival = (
                _time_of(destination, scheduled_arrival, False) or scheduled_arrival
            )
            status = _status(expected_departure, scheduled_departure)
        known.add(trip.number)
        result.append(
            Departure(
                number=trip.number,
                mode=(static.mode_of(trip.number) if static else "")
                or ("Car" if trip.road else ""),
                service_date=trip.start_date or scheduled_departure.date(),
                origin_uic=route.origin,
                destination_uic=destination.uic,
                terminus_uic=trip.stops[-1].uic if trip.stops else None,
                scheduled_departure=scheduled_departure,
                expected_departure=expected_departure,
                scheduled_arrival=scheduled_arrival,
                expected_arrival=expected_arrival,
                status=status,
                realtime=True,
                added=not is_cancelled,
                cancel_reason=CancelReason.TRAIN_CANCELLED if is_cancelled else None,
                destinations=[s.uic for s in served],
            )
        )
    return result


@dataclass(slots=True)
class RouteSnapshot:
    """Everything known about a route at one update."""

    route: RouteConfig
    window_start: datetime | None
    window_end: datetime | None
    departures: list[Departure]
    realtime: bool
    realtime_timestamp: datetime | None
    station_names: dict[str, str]

    def upcoming(self, now: datetime) -> list[Departure]:
        """Return the departures not gone yet, limited to max_trains."""
        coming = [d for d in self.departures if d.expected_departure >= now]
        return coming[: self.route.max_trains]

    def next_departure(self, now: datetime) -> Departure | None:
        """Return the next train that will actually leave."""
        return next(
            (
                d
                for d in self.departures
                if d.expected_departure >= now and not d.cancelled
            ),
            None,
        )


def build_snapshot(
    route: RouteConfig,
    static: StaticIndex,
    updates: TripUpdates | None,
    alerts: Alerts | None,
    platforms: Mapping[tuple[str, str], str] | None,
    now: datetime,
    grace: timedelta,
) -> RouteSnapshot:
    """Compute the departures of a route.

    ``updates`` is None when realtime data is missing or too old; departures
    then keep the planned times with the ``scheduled`` status.
    ``platforms`` maps (train number, scheduled departure HH:MM) to a track.
    """
    window = route.window(now)
    names = {
        uic: name
        for uic in {route.origin, *route.destinations}
        if (name := static.station_name(uic))
    }
    if window is None:
        return RouteSnapshot(route, None, None, [], updates is not None, None, names)
    start, end = window
    planned = static.scheduled_departures(
        route.origin,
        list(route.destinations),
        start,
        end,
        include_road=route.include_coaches,
    )
    departures = [merge_departure(p, updates) for p in planned]
    if updates is not None:
        departures += realtime_only_departures(
            route, updates, start, end, {d.number for d in departures}, static
        )
    for departure in departures:
        if alerts is not None:
            seen: list[str] = []
            for alert in alerts.for_train(departure.number, now):
                if alert.header and alert.header not in seen:
                    seen.append(alert.header)
            departure.alerts = seen
        if platforms:
            local = departure.scheduled_departure.astimezone(TZ_PARIS)
            departure.platform = platforms.get(
                (departure.number.lstrip("0") or "0", local.strftime("%H:%M"))
            )
        for uic in (*departure.destinations, departure.terminus_uic):
            if uic and uic not in names and (name := static.station_name(uic)):
                names[uic] = name
    departures = [d for d in departures if d.expected_departure >= now - grace]
    departures.sort(key=lambda d: (d.scheduled_departure, d.number))
    return RouteSnapshot(
        route=route,
        window_start=start,
        window_end=end,
        departures=departures,
        realtime=updates is not None,
        realtime_timestamp=updates.timestamp if updates is not None else None,
        station_names=names,
    )
