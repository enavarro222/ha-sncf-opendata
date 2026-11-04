"""Builders of synthetic SNCF open data for the tests.

The network mimics the Toulouse - Albi line, with made-up codes:

    A Toulouse Matabiau - B Saint-Sulpice - C Rabastens - D Albi Ville
                          B Saint-Sulpice - E Mazamet
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
import io
import zipfile

from google.transit import gtfs_realtime_pb2

A, B, C, D, E = "87000001", "87000002", "87000003", "87000004", "87000005"
NAMES = {
    A: "Toulouse Matabiau",
    B: "Saint-Sulpice",
    C: "Rabastens - Couffouleux",
    D: "Albi Ville",
    E: "Mazamet",
}
DAY = date(2026, 11, 4)  # a Wednesday


def stop_point(uic: str, mode: str = "Train TER") -> str:
    return f"StopPoint:OCE{mode}-{uic}"


def trip_id(number: str, suffix: str = "1") -> str:
    return f"OCESN{number}F1187_F:TER:FR:Line::X::{suffix}"


@dataclass
class Trip:
    number: str
    service: str
    # (uic, arrival, departure, pickup_type, drop_off_type)
    calls: list[tuple[str, str, str, str, str]]
    mode: str = "Train TER"
    suffix: str = "1"
    headsign: str | None = None

    @property
    def trip_id(self) -> str:
        return trip_id(self.number, self.suffix)


def default_trips() -> list[Trip]:
    return [
        # Albi train: serves Saint-Sulpice and Rabastens.
        Trip(
            "870058",
            "S1",
            [
                (A, "17:10:00", "17:10:00", "0", "1"),
                (B, "17:36:00", "17:37:00", "0", "0"),
                (C, "17:42:00", "17:43:00", "0", "0"),
                (D, "18:21:00", "18:21:00", "1", "0"),
            ],
        ),
        # Mazamet train: serves Saint-Sulpice only.
        Trip(
            "872410",
            "S1",
            [
                (A, "16:43:00", "16:43:00", "0", "1"),
                (B, "17:10:00", "17:11:00", "0", "0"),
                (E, "18:00:00", "18:00:00", "1", "0"),
            ],
        ),
        # After midnight, on the service day before.
        Trip(
            "870099",
            "S1",
            [
                (A, "25:10:00", "25:10:00", "0", "1"),
                (C, "25:40:00", "25:40:00", "1", "0"),
            ],
        ),
        # Replacement coach: not a train.
        Trip(
            "99999",
            "S1",
            [
                (A, "17:20:00", "17:20:00", "0", "1"),
                (C, "18:00:00", "18:00:00", "1", "0"),
            ],
            mode="Car TER",
        ),
        # Opposite direction.
        Trip(
            "870070",
            "S1",
            [
                (C, "17:00:00", "17:00:00", "0", "1"),
                (A, "17:40:00", "17:40:00", "1", "0"),
            ],
        ),
        # No alighting at Rabastens.
        Trip(
            "870080",
            "S1",
            [
                (A, "18:00:00", "18:00:00", "0", "1"),
                (C, "18:30:00", "18:30:00", "0", "1"),
                (D, "19:00:00", "19:00:00", "1", "0"),
            ],
        ),
        # Weekend-only train, removed on 2026-11-08.
        Trip(
            "870200",
            "S2",
            [
                (A, "09:00:00", "09:00:00", "0", "1"),
                (C, "09:30:00", "09:30:00", "1", "0"),
            ],
        ),
    ]


def make_gtfs_zip(
    trips: list[Trip] | None = None,
    *,
    feed_version: str = "2026-11-03",
    service_days: dict[str, list[date]] | None = None,
) -> bytes:
    """Build a GTFS archive."""
    trips = default_trips() if trips is None else trips
    if service_days is None:
        service_days = {
            "S1": [date(2026, 11, d) for d in range(2, 11)],
        }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        stops = [
            "stop_id,stop_name,stop_desc,stop_lat,stop_lon,zone_id,stop_url,location_type,parent_station"
        ]
        for uic, name in NAMES.items():
            stops.append(f"StopArea:OCE{uic},{name},,43.6,1.4,,,1,")
            stops.append(f"{stop_point(uic)},{name},,43.6,1.4,,,0,StopArea:OCE{uic}")
        archive.writestr("stops.txt", "\n".join(stops) + "\n")
        archive.writestr(
            "feed_info.txt",
            "feed_id,feed_publisher_name,feed_publisher_url,feed_lang,feed_start_date,feed_end_date,feed_version\n"
            f"0,SNCF,http://www.sncf.com,fr,20261103,20270430,{feed_version}\n",
        )
        rows = [
            "route_id,service_id,trip_id,trip_headsign,direction_id,block_id,shape_id"
        ]
        times = [
            "trip_id,arrival_time,departure_time,stop_id,stop_sequence,stop_headsign,pickup_type,drop_off_type,shape_dist_traveled"
        ]
        for trip in trips:
            headsign = trip.number if trip.headsign is None else trip.headsign
            rows.append(f"R1,{trip.service},{trip.trip_id},{headsign},0,,")
            for seq, (uic, arr, dep, pickup, drop) in enumerate(trip.calls):
                times.append(
                    f"{trip.trip_id},{arr},{dep},{stop_point(uic, trip.mode)},{seq},,{pickup},{drop},"
                )
        archive.writestr("trips.txt", "\n".join(rows) + "\n")
        archive.writestr("stop_times.txt", "\n".join(times) + "\n")
        dates = ["service_id,date,exception_type"]
        for service, days in service_days.items():
            dates.extend(f"{service},{d:%Y%m%d},1" for d in days)
        dates.append("S2,20261108,2")
        archive.writestr("calendar_dates.txt", "\n".join(dates) + "\n")
        archive.writestr(
            "calendar.txt",
            "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
            "S2,0,0,0,0,0,1,1,20261101,20261130\n",
        )
    return buffer.getvalue()


@dataclass
class StopTime:
    uic: str
    departure: datetime | None = None
    departure_delay: int | None = None
    arrival: datetime | None = None
    arrival_delay: int | None = None
    skipped: bool = False
    stop_area: bool = False


@dataclass
class RtTrip:
    trip_id: str
    start_date: date = DAY
    relationship: str = "SCHEDULED"
    stops: list[StopTime] = field(default_factory=list)


def make_trip_updates(trips: list[RtTrip], timestamp: datetime) -> bytes:
    """Build a GTFS-RT trip updates feed."""
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.header.gtfs_realtime_version = "2.0"
    feed.header.timestamp = int(timestamp.timestamp())
    for i, trip in enumerate(trips):
        entity = feed.entity.add()
        entity.id = str(i)
        update = entity.trip_update
        update.trip.trip_id = trip.trip_id
        update.trip.start_date = f"{trip.start_date:%Y%m%d}"
        update.trip.schedule_relationship = getattr(
            gtfs_realtime_pb2.TripDescriptor, trip.relationship
        )
        for stop in trip.stops:
            stu = update.stop_time_update.add()
            stu.stop_id = (
                f"StopArea:OCE{stop.uic}" if stop.stop_area else stop_point(stop.uic)
            )
            if stop.departure is not None:
                stu.departure.time = int(stop.departure.timestamp())
            if stop.departure_delay is not None:
                stu.departure.delay = stop.departure_delay
            if stop.arrival is not None:
                stu.arrival.time = int(stop.arrival.timestamp())
            if stop.arrival_delay is not None:
                stu.arrival.delay = stop.arrival_delay
            if stop.skipped:
                stu.schedule_relationship = (
                    gtfs_realtime_pb2.TripUpdate.StopTimeUpdate.SKIPPED
                )
    return feed.SerializeToString()


def make_alerts(
    alerts: list[
        tuple[str, list[str], dict[str, str], list[tuple[int | None, int | None]]]
    ],
    timestamp: datetime,
) -> bytes:
    """Build a GTFS-RT alerts feed from (id, trip ids, header by language, periods)."""
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.header.gtfs_realtime_version = "2.0"
    feed.header.timestamp = int(timestamp.timestamp())
    for alert_id, trips, headers, periods in alerts:
        entity = feed.entity.add()
        entity.id = alert_id
        for trip in trips:
            entity.alert.informed_entity.add().trip.trip_id = trip
        for language, text in headers.items():
            translation = entity.alert.header_text.translation.add()
            translation.language = language
            translation.text = text
            description = entity.alert.description_text.translation.add()
            description.language = language
            description.text = f"<p>{text}</p><p>D&eacute;tails&nbsp;ici.</p>"
        for start, end in periods:
            period = entity.alert.active_period.add()
            if start is not None:
                period.start = start
            if end is not None:
                period.end = end
    return feed.SerializeToString()


def utc(value: datetime) -> datetime:
    return value.astimezone(UTC)
