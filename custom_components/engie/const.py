"""Constants for the ENGIE Particuliers integration."""

from __future__ import annotations

from homeassistant.const import Platform

DOMAIN = "engie"
MANUFACTURER = "ENGIE"
ATTRIBUTION = "Data provided by ENGIE Particuliers"

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.SELECT,
    Platform.BUTTON,
]

CONF_SESSION_STORE = "session"
CONF_SCAN_INTERVAL = "scan_interval"

DEFAULT_SCAN_INTERVAL = 6 * 60 * 60  # 6 hours — tariffs barely move
TOKEN_REFRESH_SKEW = 120  # refresh JWT 2 minutes before expiry
DEFAULT_EXPIRES_IN = 3600

UNIT_EUR_KWH = "EUR/kWh"

ENERGY_LABELS = {
    "ELEC": "Electricity",
    "GAZ": "Gas",
}

HISTORY_HEAL_DAYS = 14
HISTORY_CHART_DAYS = 31
HISTORY_BACKFILL_DAYS = 400
HISTORY_MAX_DAYS = 800
CLEAR_STATISTICS_TIMEOUT_SECONDS = 30

SERVICE_IMPORT_HISTORY = "import_history"
SERVICE_CLEAR_HISTORY = "clear_import_history"

STREAM_ENERGY = "energy"
STREAM_COST = "cost"
