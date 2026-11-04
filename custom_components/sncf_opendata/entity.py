"""Base entities of the SNCF Open Data integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigSubentry
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import ATTRIBUTION, DOMAIN
from .coordinator import SncfCoordinator
from .timetable import RouteSnapshot


class SncfEntity(CoordinatorEntity[SncfCoordinator]):
    """An entity of the integration."""

    _attr_has_entity_name = True
    _attr_attribution = ATTRIBUTION


class SncfRouteEntity(SncfEntity):
    """An entity of a route; each route is a device."""

    def __init__(
        self, coordinator: SncfCoordinator, subentry: ConfigSubentry, key: str
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._route_id = subentry.subentry_id
        self._attr_translation_key = key
        self._attr_unique_id = f"{subentry.subentry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, subentry.subentry_id)},
            name=subentry.title,
            manufacturer="SNCF Voyageurs",
            model="Open data",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def snapshot(self) -> RouteSnapshot | None:
        """Return the latest data of the route."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get(self._route_id)

    @property
    def available(self) -> bool:
        """Return whether the route has data."""
        return super().available and self.snapshot is not None
