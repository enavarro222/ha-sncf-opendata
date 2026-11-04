"""Detection of the changes worth telling about a route.

This module has no Home Assistant dependency.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from .timetable import Departure, RouteSnapshot


class ChangeType(StrEnum):
    """Event types fired by the route event entity."""

    DELAYED = "delayed"
    DELAY_CHANGED = "delay_changed"
    BACK_ON_TIME = "back_on_time"
    CANCELLED = "cancelled"
    PLATFORM_ASSIGNED = "platform_assigned"
    PLATFORM_CHANGED = "platform_changed"


@dataclass(frozen=True, slots=True)
class TrainChange:
    """A change of one train."""

    change: ChangeType
    departure: Departure
    previous_delay: int | None = None
    previous_platform: str | None = None


@dataclass(slots=True)
class _Memory:
    # The delay last told about (0 while the train was announced on time).
    announced_delay: int = 0
    cancelled: bool = False
    platform: str | None = None


class ChangeTracker:
    """Compare successive snapshots of a route and report changes.

    * A delay is reported when it reaches the threshold, then again each time
      it moves by at least the threshold from the last reported value, so that
      a delay growing a minute at a time is still reported.
    * Nothing is reported from a snapshot without realtime data: a feed outage
      must not look like "back on time".
    * The first snapshot after start sets the baseline silently when it has
      realtime data, so that a restart does not repeat known disruptions.
    * Departed trains are ignored.
    """

    def __init__(self) -> None:
        """Initialize."""
        self._memory: dict[str, _Memory] = {}
        self._started = False

    def update(self, snapshot: RouteSnapshot, now: datetime) -> list[TrainChange]:
        """Return the changes since the previous snapshot."""
        threshold = snapshot.route.delay_threshold
        baseline = not self._started
        self._started = True
        changes: list[TrainChange] = []
        current = {d.key for d in snapshot.departures}
        for departure in snapshot.departures:
            memory = self._memory.setdefault(departure.key, _Memory())
            if baseline and snapshot.realtime:
                memory.cancelled = departure.cancelled
                memory.announced_delay = (
                    departure.delay if departure.delay >= threshold else 0
                )
                memory.platform = departure.platform
                continue
            if departure.expected_departure < now and not departure.cancelled:
                continue
            changes.extend(self._train_changes(departure, memory, threshold))
        # Forget the trains that left the window.
        for key in set(self._memory) - current:
            del self._memory[key]
        return changes

    @staticmethod
    def _train_changes(
        departure: Departure, memory: _Memory, threshold: int
    ) -> list[TrainChange]:
        changes: list[TrainChange] = []
        if departure.realtime:
            if departure.cancelled:
                if not memory.cancelled:
                    memory.cancelled = True
                    changes.append(TrainChange(ChangeType.CANCELLED, departure))
                return changes
            memory.cancelled = False
            delay = departure.delay
            announced = memory.announced_delay
            change: ChangeType | None = None
            if delay >= threshold and announced < threshold:
                change = ChangeType.DELAYED
            elif delay >= threshold and abs(delay - announced) >= threshold:
                change = ChangeType.DELAY_CHANGED
            elif delay < threshold <= announced:
                change = ChangeType.BACK_ON_TIME
            if change is not None:
                changes.append(TrainChange(change, departure, previous_delay=announced))
                memory.announced_delay = delay if delay >= threshold else 0
        # Platforms come from another source: a missing one means "unknown",
        # never "removed".
        if departure.platform and departure.platform != memory.platform:
            changes.append(
                TrainChange(
                    ChangeType.PLATFORM_CHANGED
                    if memory.platform
                    else ChangeType.PLATFORM_ASSIGNED,
                    departure,
                    previous_platform=memory.platform,
                )
            )
            memory.platform = departure.platform
        return changes
