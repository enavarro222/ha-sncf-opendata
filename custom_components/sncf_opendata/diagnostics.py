"""Diagnostics of the SNCF Open Data integration."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .coordinator import SncfConfigEntry
from .timetable import local_iso


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: SncfConfigEntry
) -> dict[str, Any]:
    """Return diagnostics of the config entry (no personal data involved)."""
    runtime = entry.runtime_data
    coordinator = runtime.coordinator
    index = runtime.static.index
    now = dt_util.utcnow()
    snapshots = coordinator.data or {}
    return {
        "options": dict(entry.options),
        "routes": {
            route_id: {
                **{k: v for k, v in asdict(route).items() if k != "weekdays"},
                "weekdays": sorted(route.weekdays),
                "time_start": route.time_start.isoformat(),
                "time_end": route.time_end.isoformat(),
            }
            for route_id, route in coordinator.routes.items()
        },
        "timetable": {
            "feed_version": index.feed_version if index else None,
            "checked_at": runtime.static.checked_at.isoformat()
            if runtime.static.checked_at
            else None,
            "stations": len(index.stations) if index else 0,
            "indexed_stations": sorted(index.uics) if index else [],
            "trips": len(index.trips) if index else 0,
            "date_from": index.date_from.isoformat()
            if index and index.date_from
            else None,
            "date_to": index.date_to.isoformat() if index and index.date_to else None,
        },
        "realtime": {
            "available": coordinator.realtime_available,
            "error": coordinator.realtime_error,
            "published": coordinator.trip_updates.timestamp.isoformat()
            if coordinator.trip_updates
            else None,
            "hot_routes": [r.route_id for r in coordinator.hot_routes(now)],
            "update_interval": coordinator.update_interval.total_seconds()
            if coordinator.update_interval
            else None,
            "platforms_available": coordinator.platforms.available
            if coordinator.platforms
            else None,
        },
        "snapshots": {
            route_id: {
                "window_start": local_iso(snapshot.window_start)
                if snapshot.window_start
                else None,
                "window_end": local_iso(snapshot.window_end)
                if snapshot.window_end
                else None,
                "realtime": snapshot.realtime,
                "departures": [
                    d.as_dict(snapshot.station_names) for d in snapshot.departures
                ],
            }
            for route_id, snapshot in snapshots.items()
        },
    }
