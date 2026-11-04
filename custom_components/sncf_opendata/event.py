"""Event entities of the SNCF Open Data integration."""

from __future__ import annotations

from homeassistant.components.event import EventEntity
from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .changes import ChangeTracker, ChangeType
from .const import SUBENTRY_TYPE_ROUTE
from .coordinator import SncfConfigEntry, SncfCoordinator
from .entity import SncfRouteEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SncfConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the event entities."""
    coordinator = entry.runtime_data.coordinator
    for subentry_id, subentry in entry.subentries.items():
        if subentry.subentry_type != SUBENTRY_TYPE_ROUTE:
            continue
        async_add_entities(
            [RouteEventEntity(coordinator, subentry)],
            config_subentry_id=subentry_id,
        )


class RouteEventEntity(SncfRouteEntity, EventEntity):
    """Fires when a train of the route is delayed, cancelled or changes platform."""

    _attr_event_types = [change.value for change in ChangeType]

    def __init__(self, coordinator: SncfCoordinator, subentry: ConfigSubentry) -> None:
        """Initialize."""
        super().__init__(coordinator, subentry, "train_update")
        # The first coordinator update after start is the silent baseline.
        self._tracker = ChangeTracker()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Fire an event for every change."""
        snapshot = self.snapshot
        if snapshot is not None:
            names = snapshot.station_names
            for change in self._tracker.update(snapshot, dt_util.utcnow()):
                self._trigger_event(
                    change.change.value,
                    change.departure.as_dict(names)
                    | {
                        "route": snapshot.route.name,
                        "previous_delay": change.previous_delay,
                        "previous_platform": change.previous_platform,
                    },
                )
                self.async_write_ha_state()
        self.async_write_ha_state()
