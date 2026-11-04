"""Home Assistant setup for ENGIE Particuliers."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall

try:
    from homeassistant.core import SupportsResponse
except ImportError:  # pragma: no cover - Home Assistant < 2023.8
    SupportsResponse = None
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.storage import Store

from .api import EngieAPI
from .const import (
    DOMAIN,
    HISTORY_BACKFILL_DAYS,
    HISTORY_MAX_DAYS,
    PLATFORMS,
    SERVICE_CLEAR_HISTORY,
    SERVICE_IMPORT_HISTORY,
)
from .coordinator import EngieCoordinator

_ENERGY_MAP = {
    "elec": "ELEC",
    "electricity": "ELEC",
    "electricite": "ELEC",
    "gaz": "GAZ",
    "gas": "GAZ",
    "ELEC": "ELEC",
    "GAZ": "GAZ",
}

IMPORT_SCHEMA = vol.Schema(
    {
        vol.Optional("energy"): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional("days"): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=HISTORY_MAX_DAYS)
        ),
        vol.Optional("include_costs", default=True): cv.boolean,
    }
)

CLEAR_SCHEMA = vol.Schema(
    {
        vol.Optional("energy"): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional("include_costs", default=True): cv.boolean,
    }
)


def _energies(values: list[str] | None) -> set[str] | None:
    if not values:
        return None
    mapped = {_ENERGY_MAP.get(item.strip(), item.strip().upper()) for item in values}
    mapped.discard("")
    return mapped or None


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.data.setdefault(DOMAIN, {})
    api = EngieAPI(hass, entry)
    await api.async_setup()

    coordinator = EngieCoordinator(hass, api, entry)
    await coordinator.async_config_entry_first_refresh()

    hass.data[DOMAIN][entry.entry_id] = {
        "api": api,
        "coordinator": coordinator,
    }
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    _async_register_services(hass)
    return True


def _async_register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE_IMPORT_HISTORY):
        return

    async def _import_history(call: ServiceCall) -> dict[str, Any]:
        from . import statistics as stats

        energies = _energies(call.data.get("energy"))
        days = int(call.data.get("days") or HISTORY_BACKFILL_DAYS)
        include_costs = bool(call.data.get("include_costs", True))
        imported = 0
        for runtime in list(hass.data.get(DOMAIN, {}).values()):
            if not isinstance(runtime, dict) or "coordinator" not in runtime:
                continue
            imported += await stats.async_import_history(
                hass,
                runtime["coordinator"],
                days=days,
                energies=energies,
                include_costs=include_costs,
            )
        return {"imported": imported}

    async def _clear_history(call: ServiceCall) -> dict[str, Any]:
        from . import statistics as stats

        energies = _energies(call.data.get("energy"))
        include_costs = bool(call.data.get("include_costs", True))
        cleared: list[str] = []
        for runtime in list(hass.data.get(DOMAIN, {}).values()):
            if not isinstance(runtime, dict) or "coordinator" not in runtime:
                continue
            cleared.extend(
                await stats.async_clear_history(
                    hass,
                    runtime["coordinator"],
                    energies=energies,
                    include_costs=include_costs,
                )
            )
        return {"cleared": cleared}

    register_kwargs: dict[str, Any] = {}
    if SupportsResponse is not None:
        register_kwargs["supports_response"] = SupportsResponse.OPTIONAL
    hass.services.async_register(
        DOMAIN,
        SERVICE_IMPORT_HISTORY,
        _import_history,
        schema=IMPORT_SCHEMA,
        **register_kwargs,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_CLEAR_HISTORY,
        _clear_history,
        schema=CLEAR_SCHEMA,
        **register_kwargs,
    )

    async def _probe_gas(_call: ServiceCall) -> dict[str, Any]:
        # On-demand only. No state, long-term statistics or credentials are written.
        from .gas_probe import probe_gas_readonly

        for runtime in hass.data.get(DOMAIN, {}).values():
            if isinstance(runtime, dict) and "api" in runtime:
                api = runtime["api"]
                await api.async_ensure_session()
                return await api._run(probe_gas_readonly, api._client)
        return {"error": "no_active_engie_session"}

    probe_kwargs: dict[str, Any] = {}
    if SupportsResponse is not None:
        probe_kwargs["supports_response"] = SupportsResponse.ONLY
    hass.services.async_register(
        DOMAIN, "probe_gas", _probe_gas, schema=vol.Schema({}), **probe_kwargs
    )

    async def _probe_gas_headers(_call: ServiceCall) -> dict[str, Any]:
        # Temporary test-only operation; no Home Assistant states/statistics changed.
        from .gas_headers_probe import probe_gas_mobile_headers_readonly

        for runtime in hass.data.get(DOMAIN, {}).values():
            if isinstance(runtime, dict) and "api" in runtime:
                api = runtime["api"]
                await api.async_ensure_session()
                return await api._run(probe_gas_mobile_headers_readonly, api._client)
        return {"error": "no_active_engie_session"}

    hass.services.async_register(
        DOMAIN,
        "probe_gas_headers",
        _probe_gas_headers,
        schema=vol.Schema({}),
        **probe_kwargs,
    )


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok and entry.entry_id in hass.data.get(DOMAIN, {}):
        runtime = hass.data[DOMAIN].pop(entry.entry_id)
        api: EngieAPI = runtime["api"]
        await api.async_close()
    if not any(
        isinstance(item, dict) and "coordinator" in item
        for item in hass.data.get(DOMAIN, {}).values()
    ):
        hass.services.async_remove(DOMAIN, SERVICE_IMPORT_HISTORY)
        hass.services.async_remove(DOMAIN, SERVICE_CLEAR_HISTORY)
        hass.services.async_remove(DOMAIN, "probe_gas")
        hass.services.async_remove(DOMAIN, "probe_gas_headers")
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    store = Store(hass, 1, f"engie_{entry.entry_id}")
    await store.async_remove()


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
