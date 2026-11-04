"""Constants for the SNCF Open Data integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final
from zoneinfo import ZoneInfo

DOMAIN: Final = "sncf_opendata"

ATTRIBUTION: Final = (
    "Données SNCF Voyageurs, open data (ODbL), via transport.data.gouv.fr"
)

# Times in the SNCF feeds are local to France, whatever the Home Assistant time zone.
TZ_PARIS: Final = ZoneInfo("Europe/Paris")

# Open data endpoints (no API key needed).
# The static GTFS URL is the stable data.gouv.fr resource link, which redirects
# to the file currently published by SNCF.
URL_GTFS_STATIC: Final = (
    "https://www.data.gouv.fr/api/1/datasets/r/9ae758ec-cd7a-40cd-a890-bb3963224942"
)
URL_GTFS_RT_TRIP_UPDATES: Final = (
    "https://proxy.transport.data.gouv.fr/resource/sncf-gtfs-rt-trip-updates"
)
URL_GTFS_RT_SERVICE_ALERTS: Final = (
    "https://proxy.transport.data.gouv.fr/resource/sncf-gtfs-rt-service-alerts"
)
# Unofficial: the departure board of the Gares & Connexions website (platforms).
URL_PLATFORM_BOARD: Final = (
    "https://www.garesetconnexions.sncf/schedule-table/Departures/{uic10}"
)

SUBENTRY_TYPE_ROUTE: Final = "route"

# Config entry options
CONF_REALTIME_INTERVAL: Final = "realtime_interval"
CONF_PLATFORMS: Final = "platforms"
DEFAULT_REALTIME_INTERVAL: Final = 120  # seconds
MIN_REALTIME_INTERVAL: Final = 60
MAX_REALTIME_INTERVAL: Final = 900

# Route subentry data
CONF_ORIGIN: Final = "origin"
CONF_DESTINATIONS: Final = "destinations"
CONF_ORIGIN_QUERY: Final = "origin_query"
CONF_DESTINATIONS_QUERY: Final = "destinations_query"
CONF_TIME_START: Final = "time_start"
CONF_TIME_END: Final = "time_end"
CONF_WEEKDAYS: Final = "weekdays"
CONF_DELAY_THRESHOLD: Final = "delay_threshold"
CONF_MAX_TRAINS: Final = "max_trains"
CONF_ROUTE_NAME: Final = "route_name"

WEEKDAYS: Final = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DEFAULT_WEEKDAYS: Final = ["mon", "tue", "wed", "thu", "fri"]
DEFAULT_TIME_START: Final = "07:00:00"
DEFAULT_TIME_END: Final = "09:00:00"
DEFAULT_DELAY_THRESHOLD: Final = 5  # minutes
DEFAULT_MAX_TRAINS: Final = 6

# Timings
STATIC_REFRESH_INTERVAL: Final = timedelta(hours=12)
IDLE_UPDATE_INTERVAL: Final = timedelta(minutes=10)
ALERTS_REFRESH_INTERVAL: Final = timedelta(minutes=10)
# Realtime is polled from this long before a route window opens...
HOT_BEFORE_WINDOW: Final = timedelta(minutes=60)
# ...until this long after it closes (late trains).
HOT_AFTER_WINDOW: Final = timedelta(minutes=30)
# A realtime feed older than this is ignored, never shown as current.
REALTIME_MAX_AGE: Final = timedelta(minutes=10)
# Departed trains stay in the snapshot this long, for late updates.
DEPARTED_GRACE: Final = timedelta(minutes=2)
HTTP_TIMEOUT: Final = 30  # seconds
STATIC_HTTP_TIMEOUT: Final = 180  # seconds

STORAGE_VERSION: Final = 1
STORAGE_KEY: Final = f"{DOMAIN}.static_index"
