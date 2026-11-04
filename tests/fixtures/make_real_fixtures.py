"""Cut a small regression fixture out of real SNCF open data feeds.

Usage (from the repository root, files downloaded beforehand):

    python -m tests.fixtures.make_real_fixtures GTFS.zip TRIP_UPDATES.pb \
        ALERTS.pb tests/fixtures/real

Only the trains calling at two of the stations below are kept, with all
their calls, their service days around the snapshot day, their realtime
updates and the alerts about them. The data is SNCF Voyageurs open data,
under the Open Database License (ODbL).
"""

from __future__ import annotations

import csv
import io
from pathlib import Path
import sys
import zipfile

from google.transit import gtfs_realtime_pb2

from custom_components.sncf_opendata.gtfs_static import (
    number_from_trip_id,
    uic_from_stop_id,
)

# Toulouse Matabiau, Saint-Sulpice, Rabastens - Couffouleux
STATIONS = {"87611004", "87615344", "87615328"}
FIRST_DAY, LAST_DAY = "20261103", "20261112"


def _read(archive: zipfile.ZipFile, name: str) -> tuple[list[str], list[list[str]]]:
    with archive.open(name) as raw:
        rows = list(csv.reader(io.TextIOWrapper(raw, encoding="utf-8-sig")))
    return rows[0], rows[1:]


def _csv(header: list[str], rows: list[list[str]]) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return out.getvalue()


def cut_gtfs(source: Path, target: Path) -> set[str]:
    """Write the reduced archive; return the kept train numbers."""
    with zipfile.ZipFile(source) as archive:
        st_header, stop_times = _read(archive, "stop_times.txt")
        trip_col, stop_col = st_header.index("trip_id"), st_header.index("stop_id")
        calls: dict[str, set[str]] = {}
        for row in stop_times:
            uic = uic_from_stop_id(row[stop_col])
            if uic in STATIONS:
                calls.setdefault(row[trip_col], set()).add(uic)
        trips_kept = {trip for trip, uics in calls.items() if len(uics) >= 2}
        kept_times = [row for row in stop_times if row[trip_col] in trips_kept]

        tr_header, trips = _read(archive, "trips.txt")
        tcol, scol = tr_header.index("trip_id"), tr_header.index("service_id")
        kept_trips = [row for row in trips if row[tcol] in trips_kept]
        services = {row[scol] for row in kept_trips}

        cd_header, dates = _read(archive, "calendar_dates.txt")
        kept_dates = [
            row
            for row in dates
            if row[0] in services and FIRST_DAY <= row[1] <= LAST_DAY
        ]

        uics = {uic_from_stop_id(row[stop_col]) for row in kept_times}
        s_header, stops = _read(archive, "stops.txt")
        kept_stops = [row for row in stops if uic_from_stop_id(row[0]) in uics]

        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as out:
            out.writestr("stop_times.txt", _csv(st_header, kept_times))
            out.writestr("trips.txt", _csv(tr_header, kept_trips))
            out.writestr("calendar_dates.txt", _csv(cd_header, kept_dates))
            out.writestr("stops.txt", _csv(s_header, kept_stops))
            for name in ("feed_info.txt", "agency.txt"):
                out.writestr(name, archive.read(name))
    return {number_from_trip_id(trip) for trip in trips_kept}


def cut_trip_updates(source: Path, target: Path, numbers: set[str]) -> None:
    """Keep the updates of the kept trains, and of trains added or cancelled."""
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(source.read_bytes())
    out = gtfs_realtime_pb2.FeedMessage()
    out.header.CopyFrom(feed.header)
    for entity in feed.entity:
        update = entity.trip_update
        served = {uic_from_stop_id(stop.stop_id) for stop in update.stop_time_update}
        at_stations = len(served & STATIONS) >= 2
        if number_from_trip_id(update.trip.trip_id) in numbers or at_stations:
            out.entity.add().CopyFrom(entity)
    target.write_bytes(out.SerializeToString())


def cut_alerts(source: Path, target: Path, numbers: set[str]) -> None:
    """Keep the alerts about the kept trains, in French and English."""
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(source.read_bytes())
    out = gtfs_realtime_pb2.FeedMessage()
    out.header.CopyFrom(feed.header)
    for entity in feed.entity:
        informed = [
            item
            for item in entity.alert.informed_entity
            if number_from_trip_id(item.trip.trip_id) in numbers
        ]
        if not informed:
            continue
        kept = out.entity.add()
        kept.CopyFrom(entity)
        del kept.alert.informed_entity[:]
        kept.alert.informed_entity.extend(informed)
        for text in (kept.alert.header_text, kept.alert.description_text):
            translations = [t for t in text.translation if t.language in {"fr", "en"}]
            del text.translation[:]
            text.translation.extend(translations)
    target.write_bytes(out.SerializeToString())


def main() -> None:
    """Cut the fixtures."""
    gtfs, trip_updates, alerts, out_dir = (Path(arg) for arg in sys.argv[1:5])
    out_dir.mkdir(parents=True, exist_ok=True)
    numbers = cut_gtfs(gtfs, out_dir / "gtfs.zip")
    cut_trip_updates(trip_updates, out_dir / "trip_updates.pb", numbers)
    cut_alerts(alerts, out_dir / "alerts.pb", numbers)
    print(f"{len(numbers)} trains kept")


if __name__ == "__main__":
    main()
