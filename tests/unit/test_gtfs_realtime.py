"""Tests of the realtime feeds."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.sncf_opendata.gtfs_realtime import (
    RealtimeFormatError,
    StopUpdate,
    TripRelationship,
    html_to_text,
    parse_alerts,
    parse_trip_updates,
)
from tests.builders import (
    DAY,
    A,
    B,
    C,
    RtTrip,
    StopTime,
    make_alerts,
    make_trip_updates,
    trip_id,
)

NOW = datetime(2026, 11, 4, 16, 0, tzinfo=UTC)


def test_trip_updates_index() -> None:
    dep = datetime(2026, 11, 4, 16, 15, tzinfo=UTC)
    data = make_trip_updates(
        [
            RtTrip(
                trip_id("870058"),
                stops=[
                    StopTime(A, departure=dep, departure_delay=300),
                    StopTime(B, skipped=True),
                    StopTime("not-a-uic"),
                ],
            ),
            RtTrip(
                "OCESN870010F",
                relationship="CANCELED",
                stops=[StopTime(A, stop_area=True, skipped=True)],
            ),
            RtTrip("OCESN870058F", relationship="SCHEDULED"),
            RtTrip(
                "OCESN871000F", relationship="ADDED", stops=[StopTime(A, departure=dep)]
            ),
        ],
        NOW,
    )
    updates = parse_trip_updates(data)
    assert updates.timestamp == NOW
    full = updates.find(trip_id("870058"), "870058", DAY)
    assert full is not None
    assert full.relationship is TripRelationship.SCHEDULED
    assert [s.uic for s in full.stops] == [A, B]
    origin = full.stop(A)
    assert origin == StopUpdate(A, False, None, None, int(dep.timestamp()), 300)
    assert origin.scheduled_time() == int(dep.timestamp()) - 300
    assert full.stop(B).skipped
    assert full.stop(C) is None
    # Short ids are found by train number and day.
    cancelled = updates.find("unknown", "870010", DAY)
    assert cancelled is not None
    assert cancelled.relationship is TripRelationship.CANCELED
    assert updates.find("unknown", "870010", DAY + timedelta(days=1)) is None
    assert updates.find("unknown", "", DAY) is None
    assert [t.number for t in updates.added] == ["871000"]


def test_cancellation_wins_over_update_of_same_number() -> None:
    data = make_trip_updates(
        [
            RtTrip("OCESN870010F", relationship="CANCELED"),
            RtTrip(trip_id("870010"), stops=[StopTime(A, departure_delay=0)]),
        ],
        NOW,
    )
    updates = parse_trip_updates(data)
    assert updates.by_number[("870010", DAY)].relationship is TripRelationship.CANCELED


def test_stop_update_scheduled_time() -> None:
    assert (
        StopUpdate("x", False, arrival_time=1000, arrival_delay=60).scheduled_time()
        == 940
    )
    assert StopUpdate("x", True).scheduled_time() is None


def test_invalid_feeds() -> None:
    with pytest.raises(RealtimeFormatError):
        parse_trip_updates(b"\xff\xff garbage")
    with pytest.raises(RealtimeFormatError):
        parse_trip_updates(b"")


def test_alerts() -> None:
    stamp = int(NOW.timestamp())
    data = make_alerts(
        [
            (
                "a1",
                ["OCESN870058F", "OCESN872410F"],
                {"de": "Zug", "fr": "Train complet"},
                [],
            ),
            ("a2", ["OCESN870058F"], {"en": "Works"}, [(stamp + 3600, None)]),
            ("a3", ["OCESN870058F"], {"fr": "Fini"}, [(None, stamp - 1)]),
            ("a4", ["weird"], {"fr": "Sans train"}, []),
        ],
        NOW,
    )
    alerts = parse_alerts(data, "fr")
    assert [a.header for a in alerts.for_train("870058", NOW)] == ["Train complet"]
    later = NOW + timedelta(hours=2)
    assert [a.header for a in alerts.for_train("870058", later)] == [
        "Train complet",
        "Works",
    ]
    assert alerts.for_train("1", NOW) == []
    first = alerts.alerts[0]
    assert first.description == "Train complet\nDétails ici."
    assert first.numbers == {"870058", "872410"}
    # English is picked when asked for, French otherwise.
    assert parse_alerts(data, "en").alerts[0].header == "Train complet"
    assert parse_alerts(data, "de").alerts[0].header == "Zug"


def test_html_to_text() -> None:
    assert html_to_text("<p>Le&nbsp;train</p><p>est <b>supprimé</b>.</p>") == (
        "Le train\nest supprimé."
    )
    assert html_to_text("") == ""
