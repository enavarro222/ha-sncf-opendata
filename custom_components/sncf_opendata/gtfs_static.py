"""Static GTFS (theoretical timetable) of SNCF, filtered to the stations in use.

This module has no Home Assistant dependency. The national feed holds about
90 MB of stop times once unzipped, so it is streamed and only the rows of the
stations used by the configured routes are kept. The result is small enough to
be stored as JSON and reloaded without parsing the feed again.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
import csv
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
import io
import re
from typing import IO, Any
import unicodedata
import zipfile

from .const import TZ_PARIS

UIC_RE = re.compile(r"(\d{8})$")
STOP_POINT_MODE_RE = re.compile(r"^StopPoint:OCE(.+)-\d{8}$")
# Road services (replacement buses, coaches, shuttles) are not trains.
ROAD_MODES = frozenset({"Car TER", "Car à réservation", "Car", "Navette", "Bus"})
INDEX_FORMAT = 1


class GtfsFormatError(ValueError):
    """The GTFS archive is not in the expected format."""


def uic_from_stop_id(stop_id: str) -> str | None:
    """Return the 8-digit UIC code at the end of an SNCF stop id."""
    match = UIC_RE.search(stop_id.strip())
    return match.group(1) if match else None


def mode_from_stop_id(stop_id: str) -> str:
    """Return the commercial mode encoded in a stop point id.

    ``StopPoint:OCETrain TER-87611004`` gives ``TER``,
    ``StopPoint:OCETGV INOUI-87611004`` gives ``TGV INOUI``.
    """
    match = STOP_POINT_MODE_RE.match(stop_id.strip())
    if not match:
        return ""
    mode = match.group(1)
    return "TER" if mode == "Train TER" else mode


_ABBREVIATIONS = {"st": "saint", "ste": "sainte", "gare": ""}


def normalize(text: str) -> str:
    """Lower-case and strip accents and punctuation, for station search.

    Common abbreviations are expanded, so "St Sulpice" finds "Saint-Sulpice".
    """
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    words = re.sub(r"[^a-z0-9]+", " ", stripped.lower()).split()
    return " ".join(w for w in (_ABBREVIATIONS.get(w, w) for w in words) if w)


def parse_gtfs_time(value: str) -> int | None:
    """Parse a GTFS ``HH:MM:SS`` time (hours may exceed 23) into seconds."""
    value = value.strip()
    if not value:
        return None
    parts = value.split(":")
    if len(parts) != 3:
        raise GtfsFormatError(f"Invalid GTFS time: {value!r}")
    hours, minutes, seconds = (int(p) for p in parts)
    return hours * 3600 + minutes * 60 + seconds


def service_day_start(service_date: date) -> datetime:
    """Return the reference instant (UTC) of a GTFS service day.

    GTFS times count from "noon minus 12 hours" of the service day in the
    agency time zone, which differs from midnight on daylight saving days.
    The arithmetic is done in UTC: on an aware datetime with a ZoneInfo,
    Python adds wall-clock time, which is wrong across a clock change.
    """
    noon = datetime(
        service_date.year, service_date.month, service_date.day, 12, tzinfo=TZ_PARIS
    )
    return noon.astimezone(UTC) - timedelta(hours=12)


def gtfs_datetime(service_date: date, seconds: int) -> datetime:
    """Return the instant (UTC) of a GTFS time on a service day."""
    return service_day_start(service_date) + timedelta(seconds=seconds)


@dataclass(frozen=True, slots=True)
class Station:
    """A station (GTFS stop area)."""

    uic: str
    name: str
    latitude: float | None = None
    longitude: float | None = None


@dataclass(frozen=True, slots=True)
class TripStop:
    """A call of a trip at one of the indexed stations."""

    uic: str
    arrival: int | None
    departure: int | None
    pickup: bool
    drop_off: bool


@dataclass(slots=True)
class StaticTrip:
    """A scheduled trip, restricted to its calls at indexed stations."""

    trip_id: str
    number: str
    mode: str
    service_id: str
    terminus_uic: str | None
    stops: list[TripStop]


@dataclass(slots=True)
class ScheduledDeparture:
    """A direct train from the origin to one of the destinations, as planned."""

    trip_id: str
    number: str
    mode: str
    service_date: date
    origin_uic: str
    # Configured destinations served after the origin, in calling order,
    # with their scheduled arrival time.
    destinations: list[tuple[str, datetime]]
    terminus_uic: str | None
    scheduled_departure: datetime

    @property
    def destination_uic(self) -> str:
        """Return the first configured destination served."""
        return self.destinations[0][0]

    @property
    def scheduled_arrival(self) -> datetime:
        """Return the scheduled arrival at the first destination."""
        return self.destinations[0][1]


@dataclass(slots=True)
class StaticIndex:
    """The part of the national timetable needed by the configured routes."""

    feed_version: str
    uics: frozenset[str]
    stations: dict[str, Station]
    trips: dict[str, StaticTrip] = field(default_factory=dict)
    services: dict[str, frozenset[date]] = field(default_factory=dict)
    date_from: date | None = None
    date_to: date | None = None

    def station_name(self, uic: str | None) -> str | None:
        """Return the name of a station, if known."""
        if uic is None:
            return None
        station = self.stations.get(uic)
        return station.name if station else None

    def mode_of(self, number: str) -> str:
        """Return the mode of a train number, from any of its trips."""
        return next((t.mode for t in self.trips.values() if t.number == number), "")

    def search_stations(self, query: str, limit: int = 25) -> list[Station]:
        """Return the stations whose name matches every word of the query."""
        words = normalize(query).split()
        if not words:
            return []
        matches: list[tuple[int, str, Station]] = []
        for station in self.stations.values():
            name = normalize(station.name)
            if all(word in name for word in words):
                # Exact and prefix matches first, then shorter names.
                rank = (
                    0
                    if name == " ".join(words)
                    else 1
                    if name.startswith(words[0])
                    else 2
                )
                matches.append((rank, name, station))
        matches.sort(key=lambda item: (item[0], len(item[1]), item[1]))
        return [station for _, _, station in matches[:limit]]

    def covers(self, uics: Iterable[str], date_from: date, date_to: date) -> bool:
        """Tell whether this index can answer for these stations and dates."""
        if self.date_from is None or self.date_to is None:
            return False
        return (
            set(uics) <= self.uics
            and self.date_from <= date_from
            and date_to <= self.date_to
        )

    def scheduled_departures(
        self,
        origin: str,
        destinations: list[str],
        start: datetime,
        end: datetime,
    ) -> list[ScheduledDeparture]:
        """Return direct trains leaving the origin between start and end.

        A train qualifies when passengers can board at the origin and leave at
        one of the destinations further along the trip.
        """
        wanted = set(destinations)
        first_day = (start.astimezone(TZ_PARIS) - timedelta(days=1)).date()
        last_day = end.astimezone(TZ_PARIS).date()
        result: list[ScheduledDeparture] = []
        for trip in self.trips.values():
            origin_pos = next(
                (
                    i
                    for i, stop in enumerate(trip.stops)
                    if stop.uic == origin and stop.pickup and stop.departure is not None
                ),
                None,
            )
            if origin_pos is None:
                continue
            later = [
                stop
                for stop in trip.stops[origin_pos + 1 :]
                if stop.uic in wanted and stop.drop_off
            ]
            if not later:
                continue
            origin_stop = trip.stops[origin_pos]
            assert origin_stop.departure is not None
            for service_date in self.services.get(trip.service_id, ()):
                if not first_day <= service_date <= last_day:
                    continue
                departure = gtfs_datetime(service_date, origin_stop.departure)
                if not start <= departure <= end:
                    continue
                result.append(
                    ScheduledDeparture(
                        trip_id=trip.trip_id,
                        number=trip.number,
                        mode=trip.mode,
                        service_date=service_date,
                        origin_uic=origin,
                        destinations=[
                            (
                                stop.uic,
                                gtfs_datetime(
                                    service_date,
                                    stop.arrival
                                    if stop.arrival is not None
                                    else stop.departure or 0,
                                ),
                            )
                            for stop in later
                        ],
                        terminus_uic=trip.terminus_uic,
                        scheduled_departure=departure,
                    )
                )
        result.sort(key=lambda dep: (dep.scheduled_departure, dep.number))
        return result

    def as_dict(self) -> dict[str, Any]:
        """Serialize for storage."""
        return {
            "format": INDEX_FORMAT,
            "feed_version": self.feed_version,
            "uics": sorted(self.uics),
            "date_from": self.date_from.isoformat() if self.date_from else None,
            "date_to": self.date_to.isoformat() if self.date_to else None,
            "stations": [
                [s.uic, s.name, s.latitude, s.longitude] for s in self.stations.values()
            ],
            "trips": [
                [
                    t.trip_id,
                    t.number,
                    t.mode,
                    t.service_id,
                    t.terminus_uic,
                    [
                        [s.uic, s.arrival, s.departure, s.pickup, s.drop_off]
                        for s in t.stops
                    ],
                ]
                for t in self.trips.values()
            ],
            "services": {
                sid: sorted(d.isoformat() for d in dates)
                for sid, dates in self.services.items()
            },
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StaticIndex:
        """Load a stored index. Raise ValueError if it is unusable."""
        if data.get("format") != INDEX_FORMAT:
            raise ValueError("Unsupported stored index format")
        try:
            stations = {
                row[0]: Station(row[0], row[1], row[2], row[3])
                for row in data["stations"]
            }
            trips = {
                row[0]: StaticTrip(
                    trip_id=row[0],
                    number=row[1],
                    mode=row[2],
                    service_id=row[3],
                    terminus_uic=row[4],
                    stops=[TripStop(*stop) for stop in row[5]],
                )
                for row in data["trips"]
            }
            services = {
                sid: frozenset(date.fromisoformat(d) for d in dates)
                for sid, dates in data["services"].items()
            }
            return cls(
                feed_version=data["feed_version"],
                uics=frozenset(data["uics"]),
                stations=stations,
                trips=trips,
                services=services,
                date_from=date.fromisoformat(data["date_from"])
                if data.get("date_from")
                else None,
                date_to=date.fromisoformat(data["date_to"])
                if data.get("date_to")
                else None,
            )
        except (KeyError, IndexError, TypeError) as err:
            raise ValueError(f"Corrupted stored index: {err}") from err


def _rows(archive: zipfile.ZipFile, name: str) -> Iterator[dict[str, str]]:
    """Stream the rows of a CSV file of the archive."""
    try:
        raw: IO[bytes] = archive.open(name)
    except KeyError as err:
        raise GtfsFormatError(f"Missing {name} in GTFS archive") from err
    with raw, io.TextIOWrapper(raw, encoding="utf-8-sig", newline="") as text:
        yield from csv.DictReader(text)


def _has(archive: zipfile.ZipFile, name: str) -> bool:
    return name in archive.namelist()


def _require(row: dict[str, str], *columns: str) -> None:
    missing = [c for c in columns if c not in row]
    if missing:
        raise GtfsFormatError(f"Missing GTFS columns: {', '.join(missing)}")


def read_stations(archive: zipfile.ZipFile) -> dict[str, Station]:
    """Read the stop areas (stations) of the feed."""
    stations: dict[str, Station] = {}
    checked = False
    for row in _rows(archive, "stops.txt"):
        if not checked:
            _require(row, "stop_id", "stop_name")
            checked = True
        stop_id = row["stop_id"]
        if row.get("location_type", "") != "1" and not stop_id.startswith("StopArea:"):
            continue
        uic = uic_from_stop_id(stop_id)
        if uic is None or uic in stations:
            continue
        try:
            lat: float | None = float(row["stop_lat"])
            lon: float | None = float(row["stop_lon"])
        except KeyError, ValueError:
            lat = lon = None
        stations[uic] = Station(uic, row["stop_name"].strip(), lat, lon)
    if not stations:
        raise GtfsFormatError("No station found in stops.txt")
    return stations


def read_feed_version(archive: zipfile.ZipFile) -> str:
    """Return the feed version, or an empty string."""
    if not _has(archive, "feed_info.txt"):
        return ""
    for row in _rows(archive, "feed_info.txt"):
        return row.get("feed_version", "") or row.get("feed_start_date", "")
    return ""


def _read_services(
    archive: zipfile.ZipFile, service_ids: set[str], date_from: date, date_to: date
) -> dict[str, frozenset[date]]:
    """Expand calendar.txt and calendar_dates.txt for the given services."""
    services: dict[str, set[date]] = {sid: set() for sid in service_ids}
    if _has(archive, "calendar.txt"):
        weekday_cols = (
            "monday",
            "tuesday",
            "wednesday",
            "thursday",
            "friday",
            "saturday",
            "sunday",
        )
        for row in _rows(archive, "calendar.txt"):
            sid = row.get("service_id", "")
            if sid not in services:
                continue
            first = max(
                datetime.strptime(row["start_date"], "%Y%m%d").date(), date_from
            )
            last = min(datetime.strptime(row["end_date"], "%Y%m%d").date(), date_to)
            day = first
            while day <= last:
                if row.get(weekday_cols[day.weekday()]) == "1":
                    services[sid].add(day)
                day += timedelta(days=1)
    if _has(archive, "calendar_dates.txt"):
        for row in _rows(archive, "calendar_dates.txt"):
            sid = row.get("service_id", "")
            if sid not in services:
                continue
            day = datetime.strptime(row["date"], "%Y%m%d").date()
            if not date_from <= day <= date_to:
                continue
            if row.get("exception_type") == "1":
                services[sid].add(day)
            elif row.get("exception_type") == "2":
                services[sid].discard(day)
    return {sid: frozenset(days) for sid, days in services.items() if days}


def parse_gtfs(
    archive_file: str | IO[bytes],
    uics: Iterable[str],
    date_from: date,
    date_to: date,
) -> StaticIndex:
    """Build the index of the given stations from a GTFS zip archive.

    Blocking: run it in an executor.
    """
    wanted = frozenset(uics)
    try:
        archive = zipfile.ZipFile(archive_file)
    except zipfile.BadZipFile as err:
        raise GtfsFormatError(f"Not a zip archive: {err}") from err
    with archive:
        stations = read_stations(archive)
        feed_version = read_feed_version(archive)
        index = StaticIndex(
            feed_version=feed_version,
            uics=wanted,
            stations=stations,
            date_from=date_from,
            date_to=date_to,
        )
        if not wanted:
            return index

        # Pass 1: stop times. Keep the calls at wanted stations, and the last
        # call of every trip to know its terminus.
        calls: dict[str, list[tuple[int, TripStop, str]]] = {}
        last_call: dict[str, tuple[int, str]] = {}
        checked = False
        for row in _rows(archive, "stop_times.txt"):
            if not checked:
                _require(
                    row,
                    "trip_id",
                    "arrival_time",
                    "departure_time",
                    "stop_id",
                    "stop_sequence",
                )
                checked = True
            stop_id = row["stop_id"]
            uic = uic_from_stop_id(stop_id)
            if uic is None:
                continue
            trip_id = row["trip_id"]
            sequence = int(row["stop_sequence"])
            previous = last_call.get(trip_id)
            if previous is None or sequence > previous[0]:
                last_call[trip_id] = (sequence, uic)
            if uic not in wanted:
                continue
            calls.setdefault(trip_id, []).append(
                (
                    sequence,
                    TripStop(
                        uic=uic,
                        arrival=parse_gtfs_time(row["arrival_time"]),
                        departure=parse_gtfs_time(row["departure_time"]),
                        pickup=row.get("pickup_type", "0") != "1",
                        drop_off=row.get("drop_off_type", "0") != "1",
                    ),
                    mode_from_stop_id(stop_id),
                )
            )

        # Only trips calling at two wanted stations can make a route.
        candidates = {
            trip_id: sorted(rows, key=lambda r: r[0])
            for trip_id, rows in calls.items()
            if len({r[1].uic for r in rows}) >= 2
        }
        del calls

        # Pass 2: trips (train number, service).
        service_ids: set[str] = set()
        for row in _rows(archive, "trips.txt"):
            trip_id = row.get("trip_id", "")
            rows = candidates.get(trip_id)
            if rows is None:
                continue
            modes = [r[2] for r in rows if r[2]]
            mode = modes[0] if modes else ""
            if mode in ROAD_MODES:
                continue
            number = (row.get("trip_headsign") or "").strip() or _number_from_trip_id(
                trip_id
            )
            terminus = last_call.get(trip_id)
            index.trips[trip_id] = StaticTrip(
                trip_id=trip_id,
                number=number,
                mode=mode,
                service_id=row["service_id"],
                terminus_uic=terminus[1] if terminus else None,
                stops=[r[1] for r in rows],
            )
            service_ids.add(row["service_id"])

        # Pass 3: service days.
        index.services = _read_services(archive, service_ids, date_from, date_to)
        # Trips that never run in the date range are useless.
        index.trips = {
            trip_id: trip
            for trip_id, trip in index.trips.items()
            if trip.service_id in index.services
        }
        return index


_TRIP_NUMBER_RE = re.compile(r"^OCESN(\d+)F")


def _number_from_trip_id(trip_id: str) -> str:
    match = _TRIP_NUMBER_RE.match(trip_id)
    return match.group(1) if match else ""


def number_from_trip_id(trip_id: str) -> str:
    """Return the train number encoded in an SNCF trip id."""
    return _number_from_trip_id(trip_id)
