"""Binary sensors of the SNCF Open Data integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.components.binary_sensor.const import BinarySensorDeviceClass
from homeassistant.config_entries import ConfigSubentry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import DOMAIN, SUBENTRY_TYPE_ROUTE
from .coordinator import SncfConfigEntry, SncfCoordinator
from .entity import SncfEntity, SncfRouteEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SncfConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the binary sensors."""
    coordinator = entry.runtime_data.coordinator
    async_add_entities([RealtimeFeedBinarySensor(coordinator, entry.entry_id)])
    for subentry_id, subentry in entry.subentries.items():
        if subentry.subentry_type != SUBENTRY_TYPE_ROUTE:
            continue
        async_add_entities(
            [DisruptionBinarySensor(coordinator, subentry)],
            config_subentry_id=subentry_id,
        )


class DisruptionBinarySensor(SncfRouteEntity, BinarySensorEntity):
    """On when a train of the window is cancelled or late beyond the threshold."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _unrecorded_attributes = frozenset({"cancelled", "delayed"})

    def __init__(self, coordinator: SncfCoordinator, subentry: ConfigSubentry) -> None:
        """Initialize."""
        super().__init__(coordinator, subentry, "disruption")

    def _disrupted(self) -> tuple[list[str], list[str]]:
        snapshot = self.snapshot
        if snapshot is None:
            return [], []
        threshold = snapshot.route.delay_threshold
        coming = [
            d for d in snapshot.departures if d.expected_departure >= dt_util.utcnow()
        ]
        cancelled = [d.number for d in coming if d.cancelled]
        delayed = [d.number for d in coming if not d.cancelled and d.delay >= threshold]
        return cancelled, delayed

    @property
    def is_on(self) -> bool | None:
        """Return whether the route is disrupted."""
        if self.snapshot is None:
            return None
        cancelled, delayed = self._disrupted()
        return bool(cancelled or delayed)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the trains concerned."""
        cancelled, delayed = self._disrupted()
        return {"cancelled": cancelled, "delayed": delayed}


class RealtimeFeedBinarySensor(SncfEntity, BinarySensorEntity):
    """Whether the SNCF realtime feed answers with recent data."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "realtime_feed"

    def __init__(self, coordinator: SncfCoordinator, entry_id: str) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry_id}_realtime_feed"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name="SNCF Open Data",
            manufacturer="SNCF Voyageurs",
            model="Open data",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def is_on(self) -> bool | None:
        """Return the state of the feed (unknown until first needed)."""
        return self.coordinator.realtime_available

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return details on the feed."""
        updates = self.coordinator.trip_updates
        platforms = self.coordinator.platforms
        return {
            "published": updates.timestamp.isoformat() if updates else None,
            "error": self.coordinator.realtime_error,
            "timetable_version": self.coordinator.static.index.feed_version
            if self.coordinator.static.index
            else None,
            "platforms_available": platforms.available if platforms else None,
        }
