"""Sensors of the SNCF Open Data integration."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import SUBENTRY_TYPE_ROUTE
from .coordinator import SncfConfigEntry, SncfCoordinator
from .entity import SncfRouteEntity
from .timetable import local_iso

# Coordinator based: no polling of its own.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SncfConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the sensors."""
    coordinator = entry.runtime_data.coordinator
    for subentry_id, subentry in entry.subentries.items():
        if subentry.subentry_type != SUBENTRY_TYPE_ROUTE:
            continue
        async_add_entities(
            [NextDepartureSensor(coordinator, subentry)],
            config_subentry_id=subentry_id,
        )


class NextDepartureSensor(SncfRouteEntity, SensorEntity):
    """Expected departure time of the next train that runs.

    The attributes list every train of the time window, cancelled ones
    included, so that automations and cards can show the whole picture.
    """

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    # The list of trains changes at every update: keep it out of the database.
    _unrecorded_attributes = frozenset(
        {
            "trains",
            "window_start",
            "window_end",
            "realtime",
            "realtime_updated",
            "train_number",
            "scheduled_departure",
            "delay",
            "status",
            "platform",
            "destinations",
        }
    )

    def __init__(self, coordinator: SncfCoordinator, subentry: ConfigSubentry) -> None:
        """Initialize."""
        super().__init__(coordinator, subentry, "next_departure")

    @property
    def native_value(self) -> datetime | None:
        """Return the expected departure of the next train."""
        if (snapshot := self.snapshot) is None:
            return None
        departure = snapshot.next_departure(dt_util.utcnow())
        return departure.expected_departure if departure else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return the trains of the window."""
        if (snapshot := self.snapshot) is None:
            return None
        now = dt_util.utcnow()
        names = snapshot.station_names
        attributes: dict[str, Any] = {
            "trains": [d.as_dict(names) for d in snapshot.upcoming(now)],
            "window_start": local_iso(snapshot.window_start)
            if snapshot.window_start
            else None,
            "window_end": local_iso(snapshot.window_end)
            if snapshot.window_end
            else None,
            "realtime": snapshot.realtime,
            "realtime_updated": snapshot.realtime_timestamp.isoformat()
            if snapshot.realtime_timestamp
            else None,
        }
        if (departure := snapshot.next_departure(now)) is not None:
            attributes |= {
                "train_number": departure.number,
                "scheduled_departure": local_iso(departure.scheduled_departure),
                "delay": departure.delay,
                "status": departure.status.value,
                "platform": departure.platform,
                "destinations": [names.get(u, u) for u in departure.destinations],
            }
        return attributes
