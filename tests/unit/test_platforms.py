"""Tests of the Gares & Connexions platform client."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import logging

import aiohttp
import pytest

from custom_components.sncf_opendata.platforms import (
    PlatformClient,
    parse_board,
    train_key,
)

NOW = datetime(2026, 11, 4, 16, 0, tzinfo=UTC)


def board_item(
    number: object = "870058",
    scheduled: object = "2026-11-04T16:10:00+00:00",
    track: object = "1C",
) -> dict:
    return {
        "trainNumber": number,
        "scheduledTime": scheduled,
        "platform": {"track": track, "isTrackactive": True},
    }


def test_parse_board() -> None:
    payload = [
        board_item(),
        board_item("0871417", "2026-11-04T18:49:00Z", "B"),
        board_item(871418, "2026-11-04T19:49:00", " 12 "),
        board_item("1", track=None),
        board_item("2", track="<script>"),
        board_item("3", scheduled="tomorrow"),
        board_item("4", scheduled=None),
        {"trainNumber": "5", "scheduledTime": "2026-11-04T16:10:00Z", "platform": "1"},
        "junk",
    ]
    assert parse_board(payload) == {
        ("870058", "17:10"): "1C",
        ("871417", "19:49"): "B",
        # Naive times are taken as French local time.
        ("871418", "19:49"): "12",
    }
    with pytest.raises(ValueError, match="list"):
        parse_board({"error": "nope"})
    assert train_key("000") == "0"


class FakeResponse:
    def __init__(self, payload: object, status: int = 200) -> None:
        self._payload = payload
        self.status = status

    async def __aenter__(self) -> FakeResponse:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise aiohttp.ClientError(f"HTTP {self.status}")

    async def json(self, content_type: str | None = None) -> object:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeSession:
    def __init__(self) -> None:
        self.responses: list[FakeResponse] = []
        self.urls: list[str] = []

    def get(self, url: str, **kwargs: object) -> FakeResponse:
        self.urls.append(url)
        return self.responses.pop(0)


async def test_client_cache_backoff_and_logs(caplog: pytest.LogCaptureFixture) -> None:
    session = FakeSession()
    client = PlatformClient(session)  # type: ignore[arg-type]
    session.responses.append(FakeResponse([board_item()]))
    assert await client.async_platforms("87611004", NOW) == {("870058", "17:10"): "1C"}
    assert session.urls == [
        "https://www.garesetconnexions.sncf/schedule-table/Departures/0087611004"
    ]
    # Failure: the cached answer is still served, one warning is logged.
    session.responses.append(FakeResponse(None, status=503))
    caplog.set_level(logging.DEBUG)
    later = NOW + timedelta(minutes=2)
    assert await client.async_platforms("87611004", later) == {
        ("870058", "17:10"): "1C"
    }
    assert not client.available
    assert (
        sum(
            "unavailable" in r.message
            for r in caplog.records
            if r.levelno == logging.WARNING
        )
        == 1
    )
    # Within the back-off delay, no request is made.
    assert await client.async_platforms("87611004", later + timedelta(minutes=1)) == {
        ("870058", "17:10"): "1C"
    }
    assert len(session.urls) == 2
    # After the back-off, a garbage answer fails again, without a new warning.
    session.responses.append(FakeResponse(ValueError("not json")))
    assert await client.async_platforms("87611004", later + timedelta(minutes=3)) == {
        ("870058", "17:10"): "1C"
    }
    assert sum(r.levelno == logging.WARNING for r in caplog.records) == 1
    # The cache expires: nothing rather than stale platforms.
    session.responses.append(FakeResponse(None, status=500))
    assert await client.async_platforms("87611004", NOW + timedelta(minutes=20)) == {}
    # Back: an info line, and fresh data.
    session.responses.append(FakeResponse([board_item(track="2")]))
    assert await client.async_platforms("87611004", NOW + timedelta(minutes=40)) == {
        ("870058", "17:10"): "2"
    }
    assert client.available
    assert any("available again" in r.message for r in caplog.records)
