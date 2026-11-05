"""GTFS-RT feeds of SNCF: trip updates (delays, cancellations) and alerts.

This module has no Home Assistant dependency.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from enum import StrEnum
import html
import re

from google.protobuf.message import DecodeError
from google.transit import gtfs_realtime_pb2

from .gtfs_static import is_road_trip_id, number_from_trip_id, uic_from_stop_id

_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")


class RealtimeFormatError(ValueError):
    """A GTFS-RT feed could not be decoded."""


class TripRelationship(StrEnum):
    """Schedule relationship of a trip in the realtime feed."""

    SCHEDULED = "scheduled"
    CANCELED = "canceled"
    ADDED = "added"
    OTHER = "other"


_TRIP_RELATIONSHIPS = {
    gtfs_realtime_pb2.TripDescriptor.SCHEDULED: TripRelationship.SCHEDULED,
    gtfs_realtime_pb2.TripDescriptor.CANCELED: TripRelationship.CANCELED,
    gtfs_realtime_pb2.TripDescriptor.ADDED: TripRelationship.ADDED,
}
_STOP_SKIPPED = gtfs_realtime_pb2.TripUpdate.StopTimeUpdate.SKIPPED


@dataclass(frozen=True, slots=True)
class StopUpdate:
    """Realtime information for one call of a trip."""

    uic: str
    skipped: bool
    arrival_time: int | None = None  # POSIX timestamp
    arrival_delay: int | None = None  # seconds
    departure_time: int | None = None
    departure_delay: int | None = None

    def scheduled_time(self) -> int | None:
        """Return the planned time of this call, from time minus delay."""
        for value, delay in (
            (self.departure_time, self.departure_delay),
            (self.arrival_time, self.arrival_delay),
        ):
            if value is not None:
                return value - (delay or 0)
        return None


@dataclass(slots=True)
class RealtimeTrip:
    """A trip of the realtime feed."""

    trip_id: str
    number: str
    start_date: date | None
    relationship: TripRelationship
    stops: list[StopUpdate]
    # A road service (coach, bus), from the trip id.
    road: bool = False

    def stop(self, uic: str) -> StopUpdate | None:
        """Return the update of a call, if any."""
        return next((s for s in self.stops if s.uic == uic), None)


@dataclass(slots=True)
class TripUpdates:
    """Index of the trip updates feed."""

    timestamp: datetime
    by_trip: dict[tuple[str, date | None], RealtimeTrip] = field(default_factory=dict)
    by_number: dict[tuple[str, date | None], RealtimeTrip] = field(default_factory=dict)
    added: list[RealtimeTrip] = field(default_factory=list)

    def find(
        self, trip_id: str, number: str, service_date: date
    ) -> RealtimeTrip | None:
        """Find the realtime trip of a scheduled train.

        The feed uses the full trip id for updates of planned trains, but a
        short ``OCESN<number>F`` (or ``R``) id for cancellations and added trains, so the
        train number and service day are used as a fallback.
        """
        trip = self.by_trip.get((trip_id, service_date))
        if trip is None and number:
            trip = self.by_number.get((number, service_date))
        return trip


def _parse_date(value: str) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y%m%d").date()
    except ValueError:
        return None


def _decode(data: bytes) -> gtfs_realtime_pb2.FeedMessage:
    feed = gtfs_realtime_pb2.FeedMessage()
    try:
        feed.ParseFromString(data)
    except DecodeError as err:
        raise RealtimeFormatError(f"Invalid GTFS-RT feed: {err}") from err
    if not feed.HasField("header"):
        raise RealtimeFormatError("GTFS-RT feed without header")
    return feed


def _timestamp(feed: gtfs_realtime_pb2.FeedMessage) -> datetime:
    return datetime.fromtimestamp(feed.header.timestamp, tz=UTC)


def parse_trip_updates(data: bytes) -> TripUpdates:
    """Decode the trip updates feed. Blocking: run it in an executor."""
    feed = _decode(data)
    result = TripUpdates(timestamp=_timestamp(feed))
    for entity in feed.entity:
        if not entity.HasField("trip_update"):
            continue
        update = entity.trip_update
        descriptor = update.trip
        stops: list[StopUpdate] = []
        for stop in update.stop_time_update:
            uic = uic_from_stop_id(stop.stop_id)
            if uic is None:
                continue
            has_arrival = stop.HasField("arrival")
            has_departure = stop.HasField("departure")
            stops.append(
                StopUpdate(
                    uic=uic,
                    skipped=stop.schedule_relationship == _STOP_SKIPPED,
                    arrival_time=stop.arrival.time
                    if has_arrival and stop.arrival.HasField("time")
                    else None,
                    arrival_delay=stop.arrival.delay
                    if has_arrival and stop.arrival.HasField("delay")
                    else None,
                    departure_time=stop.departure.time
                    if has_departure and stop.departure.HasField("time")
                    else None,
                    departure_delay=stop.departure.delay
                    if has_departure and stop.departure.HasField("delay")
                    else None,
                )
            )
        trip = RealtimeTrip(
            trip_id=descriptor.trip_id,
            number=number_from_trip_id(descriptor.trip_id),
            start_date=_parse_date(descriptor.start_date),
            relationship=_TRIP_RELATIONSHIPS.get(
                descriptor.schedule_relationship, TripRelationship.OTHER
            ),
            stops=stops,
            road=is_road_trip_id(descriptor.trip_id),
        )
        if trip.relationship is TripRelationship.ADDED:
            result.added.append(trip)
            continue
        result.by_trip[(trip.trip_id, trip.start_date)] = trip
        if trip.number:
            key = (trip.number, trip.start_date)
            existing = result.by_number.get(key)
            # A cancellation wins over a plain update of the same train.
            if existing is None or trip.relationship is TripRelationship.CANCELED:
                result.by_number[key] = trip
    return result


@dataclass(frozen=True, slots=True)
class Alert:
    """A traveller information message."""

    alert_id: str
    header: str
    description: str
    periods: tuple[tuple[int | None, int | None], ...]
    numbers: frozenset[str]
    uics: frozenset[str]

    def active(self, now: datetime) -> bool:
        """Tell whether the alert applies at this instant."""
        if not self.periods:
            return True
        stamp = now.timestamp()
        return any(
            (start is None or start <= stamp) and (end is None or stamp <= end)
            for start, end in self.periods
        )


@dataclass(slots=True)
class Alerts:
    """Index of the service alerts feed."""

    timestamp: datetime
    alerts: list[Alert] = field(default_factory=list)
    by_number: dict[str, list[Alert]] = field(default_factory=dict)

    def for_train(self, number: str, now: datetime) -> list[Alert]:
        """Return the active alerts about a train."""
        return [a for a in self.by_number.get(number, []) if a.active(now)]


def _translation(text: gtfs_realtime_pb2.TranslatedString, language: str) -> str:
    """Pick the translation in the wanted language, else French, else the first."""
    translations = list(text.translation)
    if not translations:
        return ""
    for wanted in (language, "fr"):
        for item in translations:
            if item.language == wanted:
                return str(item.text)
    return str(translations[0].text)


def html_to_text(value: str) -> str:
    """Turn the small HTML snippets of the feed into plain text."""
    value = re.sub(r"</p>\s*<p>|<br\s*/?>", "\n", value, flags=re.IGNORECASE)
    value = html.unescape(_TAG_RE.sub("", value))
    lines = [_SPACE_RE.sub(" ", line).strip() for line in value.splitlines()]
    return "\n".join(line for line in lines if line)


def parse_alerts(data: bytes, language: str = "fr") -> Alerts:
    """Decode the service alerts feed. Blocking: run it in an executor."""
    feed = _decode(data)
    result = Alerts(timestamp=_timestamp(feed))
    for entity in feed.entity:
        if not entity.HasField("alert"):
            continue
        alert = entity.alert
        numbers = set()
        uics = set()
        for informed in alert.informed_entity:
            if informed.HasField("trip") and informed.trip.trip_id:
                number = number_from_trip_id(informed.trip.trip_id)
                if number:
                    numbers.add(number)
            if informed.stop_id:
                uic = uic_from_stop_id(informed.stop_id)
                if uic:
                    uics.add(uic)
        item = Alert(
            alert_id=entity.id,
            header=html_to_text(_translation(alert.header_text, language)),
            description=html_to_text(_translation(alert.description_text, language)),
            periods=tuple(
                (
                    period.start if period.HasField("start") else None,
                    period.end if period.HasField("end") else None,
                )
                for period in alert.active_period
            ),
            numbers=frozenset(numbers),
            uics=frozenset(uics),
        )
        if not item.header and not item.description:
            continue
        result.alerts.append(item)
        for number in numbers:
            result.by_number.setdefault(number, []).append(item)
    return result
