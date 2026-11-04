"""Tests of the static timetable."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
import io
import zipfile

import pytest

from custom_components.sncf_opendata.const import TZ_PARIS
from custom_components.sncf_opendata.gtfs_static import (
    GtfsFormatError,
    StaticIndex,
    gtfs_datetime,
    mode_from_stop_id,
    normalize,
    parse_gtfs,
    parse_gtfs_time,
    service_day_start,
    uic_from_stop_id,
)
from tests.builders import DAY, A, B, C, D, E, Trip, make_gtfs_zip

FIRST, LAST = date(2026, 11, 3), date(2026, 11, 12)


def index_of(data: bytes, uics: set[str]) -> StaticIndex:
    return parse_gtfs(io.BytesIO(data), uics, FIRST, LAST)


def at(day: date, hhmm: str) -> datetime:
    return datetime.combine(day, time.fromisoformat(hhmm), tzinfo=TZ_PARIS)


def test_stop_id_helpers() -> None:
    assert uic_from_stop_id("StopPoint:OCETrain TER-87611004") == "87611004"
    assert uic_from_stop_id("StopArea:OCE87611004") == "87611004"
    assert uic_from_stop_id("weird") is None
    assert mode_from_stop_id("StopPoint:OCETrain TER-87611004") == "TER"
    assert mode_from_stop_id("StopPoint:OCETGV INOUI-87611004") == "TGV INOUI"
    assert mode_from_stop_id("StopArea:OCE87611004") == ""


def test_times_and_service_days() -> None:
    assert parse_gtfs_time("25:10:00") == 25 * 3600 + 600
    assert parse_gtfs_time("") is None
    with pytest.raises(GtfsFormatError):
        parse_gtfs_time("12:00")
    assert service_day_start(DAY) == at(DAY, "00:00")
    assert gtfs_datetime(DAY, 17 * 3600 + 600) == at(DAY, "17:10")
    assert gtfs_datetime(DAY, 25 * 3600 + 600) == at(DAY + timedelta(days=1), "01:10")


def test_dst_service_day() -> None:
    # On 2026-10-25 France goes from UTC+2 to UTC+1: noon - 12 h is 01:00.
    start = service_day_start(date(2026, 10, 25))
    assert start.astimezone(TZ_PARIS).hour == 1


def test_normalize_and_search() -> None:
    assert normalize("Saint-Sulpice (Tarn)") == "saint sulpice tarn"
    assert normalize("St Sulpice") == "saint sulpice"
    assert normalize("Gare de Lisle-sur-Tarn") == "de lisle sur tarn"
    index = index_of(make_gtfs_zip(), set())
    assert [s.uic for s in index.search_stations("st sulpice")] == [B]
    assert [s.uic for s in index.search_stations("matabiau")] == [A]
    assert [s.uic for s in index.search_stations("RABASTENS")] == [C]
    assert index.search_stations("   ") == []
    assert index.search_stations("nowhere") == []
    assert len(index.stations) == 5


def test_index_keeps_direct_trains_only() -> None:
    index = index_of(make_gtfs_zip(), {A, B, C})
    numbers = sorted(t.number for t in index.trips.values())
    # The replacement coach is not a train.
    assert "99999" not in numbers
    assert {"870058", "872410", "870099", "870070", "870080", "870200"} <= set(numbers)
    albi = next(t for t in index.trips.values() if t.number == "870058")
    assert albi.mode == "TER"
    assert albi.terminus_uic == D
    assert [s.uic for s in albi.stops] == [A, B, C]


def test_scheduled_departures_rules() -> None:
    index = index_of(make_gtfs_zip(), {A, B, C})
    deps = index.scheduled_departures(A, [C], at(DAY, "16:00"), at(DAY, "20:00"))
    # 870080 cannot drop off at C, 872410 does not go to C, coach excluded,
    # 870070 goes the other way.
    assert [d.number for d in deps] == ["870058"]
    dep = deps[0]
    assert dep.scheduled_departure == at(DAY, "17:10")
    assert dep.destination_uic == C
    assert dep.scheduled_arrival == at(DAY, "17:42")
    assert dep.terminus_uic == D

    both = index.scheduled_departures(A, [C, B], at(DAY, "16:00"), at(DAY, "20:00"))
    assert [d.number for d in both] == ["872410", "870058"]
    # Destinations come in calling order, whatever the configured order.
    assert [uic for uic, _ in both[1].destinations] == [B, C]


def test_after_midnight_and_calendar() -> None:
    index = index_of(make_gtfs_zip(), {A, C})
    night = index.scheduled_departures(
        A,
        [C],
        at(DAY + timedelta(days=1), "00:30"),
        at(DAY + timedelta(days=1), "02:00"),
    )
    assert [(d.number, d.service_date) for d in night] == [("870099", DAY)]
    # calendar.txt weekends, minus the exception of 2026-11-08.
    saturday, sunday = date(2026, 11, 7), date(2026, 11, 8)
    assert [
        d.number
        for d in index.scheduled_departures(
            A, [C], at(saturday, "08:00"), at(saturday, "10:00")
        )
    ] == ["870200"]
    assert (
        index.scheduled_departures(A, [C], at(sunday, "08:00"), at(sunday, "10:00"))
        == []
    )


def test_headsign_fallback_and_unused_trips() -> None:
    trips = [
        Trip(
            "870300",
            "S1",
            [
                (A, "10:00:00", "10:00:00", "0", "1"),
                (C, "10:30:00", "10:30:00", "1", "0"),
            ],
            headsign="",
        ),
        Trip(
            "870301",
            "NEVER",
            [
                (A, "11:00:00", "11:00:00", "0", "1"),
                (C, "11:30:00", "11:30:00", "1", "0"),
            ],
        ),
    ]
    index = index_of(make_gtfs_zip(trips), {A, C})
    assert [t.number for t in index.trips.values()] == ["870300"]


def test_roundtrip_and_covers() -> None:
    index = index_of(make_gtfs_zip(), {A, B, C})
    restored = StaticIndex.from_dict(index.as_dict())
    assert restored.trips.keys() == index.trips.keys()
    assert restored.services == index.services
    assert restored.feed_version == "2026-11-03"
    assert restored.covers({A, C}, FIRST, LAST)
    assert not restored.covers({A, D}, FIRST, LAST)
    assert not restored.covers({A}, FIRST, LAST + timedelta(days=1))
    with pytest.raises(ValueError, match="format"):
        StaticIndex.from_dict({"format": 99})
    data = index.as_dict()
    del data["trips"]
    with pytest.raises(ValueError, match="Corrupted"):
        StaticIndex.from_dict(data)


def test_no_wanted_station() -> None:
    index = index_of(make_gtfs_zip(), set())
    assert index.trips == {}
    assert index.station_name(A) == "Toulouse Matabiau"
    assert index.station_name(None) is None
    assert index.station_name("00000000") is None


def test_mode_of() -> None:
    index = index_of(make_gtfs_zip(), {A, B, C, E})
    assert index.mode_of("872410") == "TER"
    assert index.mode_of("1") == ""


def test_bad_archives() -> None:
    with pytest.raises(GtfsFormatError, match="zip"):
        index_of(b"not a zip", {A})
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("stops.txt", "stop_id,stop_name,location_type\n")
    with pytest.raises(GtfsFormatError, match="No station"):
        index_of(buffer.getvalue(), {A})
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "stops.txt",
            "stop_id,stop_name,location_type\nStopArea:OCE87000001,X,1\n",
        )
    with pytest.raises(GtfsFormatError, match="stop_times"):
        index_of(buffer.getvalue(), {A})
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "stops.txt",
            "stop_id,stop_name,location_type\nStopArea:OCE87000001,X,1\n",
        )
        archive.writestr("stop_times.txt", "trip_id,stop_id\nT,StopArea:OCE87000001\n")
    with pytest.raises(GtfsFormatError, match="columns"):
        index_of(buffer.getvalue(), {A})
