"""Platforms from the Gares & Connexions departure boards (optional, unofficial).

Platforms are not in the SNCF open data. The Gares & Connexions website shows
them on its station departure boards, through an endpoint that is not a
documented API: it may change or disappear at any time. This client is built
so that such a failure never affects the rest of the integration:

* strict validation of the answer, anything unexpected is ignored;
* a short timeout, and growing back-off between attempts after failures;
* the last good answer is reused for a while, then dropped (never stale);
* one log line when the source becomes unavailable, one when it is back.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import logging
import re
from typing import Any

import aiohttp

from .const import TZ_PARIS, URL_PLATFORM_BOARD

_LOGGER = logging.getLogger(__name__)

TIMEOUT = aiohttp.ClientTimeout(total=15)
CACHE_TTL = timedelta(minutes=15)
MAX_BACKOFF = timedelta(minutes=30)
_TRACK_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z ./-]{0,7}$")

type PlatformMap = dict[tuple[str, str], str]


def train_key(number: str) -> str:
    """Normalize a train number for matching."""
    return number.strip().lstrip("0") or "0"


def parse_board(payload: Any) -> PlatformMap:
    """Extract (train number, scheduled HH:MM local) -> track from a board.

    Raise ValueError when the payload does not look like a departure board.
    """
    if not isinstance(payload, list):
        raise ValueError("Departure board is not a list")
    result: PlatformMap = {}
    for item in payload:
        if not isinstance(item, Mapping):
            continue
        number = item.get("trainNumber")
        scheduled = item.get("scheduledTime")
        platform = item.get("platform")
        if not isinstance(number, (str, int)) or not isinstance(scheduled, str):
            continue
        if not isinstance(platform, Mapping):
            continue
        track = platform.get("track")
        if not isinstance(track, str) or not _TRACK_RE.match(track.strip()):
            continue
        try:
            when = datetime.fromisoformat(scheduled.replace("Z", "+00:00"))
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=TZ_PARIS)
        local = when.astimezone(TZ_PARIS).strftime("%H:%M")
        result[(train_key(str(number)), local)] = track.strip()
    return result


@dataclass(slots=True)
class _StationState:
    platforms: PlatformMap = field(default_factory=dict)
    fetched_at: datetime | None = None
    failures: int = 0
    retry_at: datetime | None = None


class PlatformClient:
    """Fetch platforms for stations, without ever raising."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        """Initialize the client."""
        self._session = session
        self._stations: dict[str, _StationState] = {}
        self._available = True

    @property
    def available(self) -> bool:
        """Tell whether the last attempts succeeded."""
        return self._available

    async def async_platforms(self, uic: str, now: datetime) -> PlatformMap:
        """Return the known platforms of a station (maybe empty)."""
        state = self._stations.setdefault(uic, _StationState())
        if state.retry_at is None or now >= state.retry_at:
            await self._async_refresh(uic, state, now)
        if state.fetched_at is not None and now - state.fetched_at <= CACHE_TTL:
            return state.platforms
        return {}

    async def _async_refresh(
        self, uic: str, state: _StationState, now: datetime
    ) -> None:
        url = URL_PLATFORM_BOARD.format(uic10=uic.zfill(10))
        try:
            async with self._session.get(
                url, timeout=TIMEOUT, headers={"Accept": "application/json"}
            ) as response:
                response.raise_for_status()
                payload = await response.json(content_type=None)
            platforms = parse_board(payload)
        except Exception as err:
            # Whatever happens with this unofficial source, it must never
            # break the update of the departures.
            state.failures += 1
            state.retry_at = now + min(
                timedelta(minutes=2**state.failures), MAX_BACKOFF
            )
            if self._available:
                self._available = False
                _LOGGER.warning(
                    "Platforms unavailable from Gares & Connexions (%s): %s. "
                    "Departures are shown without platforms until it works again",
                    uic,
                    err,
                )
            else:
                _LOGGER.debug("Platforms still unavailable for %s: %s", uic, err)
            return
        if not self._available:
            self._available = True
            _LOGGER.info("Platforms available again from Gares & Connexions")
        state.platforms = platforms
        state.fetched_at = now
        state.failures = 0
        state.retry_at = None
