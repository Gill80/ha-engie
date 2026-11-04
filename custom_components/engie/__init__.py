"""Home Assistant setup for ENGIE Particuliers."""

from __future__ import annotations

from typing import Any
from pathlib import Path

import voluptuous as vol

from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError

try:
    from homeassistant.core import SupportsResponse
except ImportError:  # pragma: no cover - Home Assistant < 2023.8
    SupportsResponse = None
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntry
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

# User-triggered import is always scoped to ENGIE physical meters; a selected
# entity is checked against this integration's own entity registry IDs.
METER_HISTORY_SCHEMA = vol.Schema({
    vol.Optional("meter_entity"): cv.entity_id,
    vol.Optional("meter_source_id"): vol.Match(r"^meter_[0-9a-f]{20}$"),
    vol.Optional("period", default="365_days"): vol.In((
        "90_days", "365_days", "all_available", "custom",
    )),
    vol.Optional("start_date"): cv.string,
    vol.Optional("end_date"): cv.string,
    vol.Optional("include_costs", default=True): cv.boolean,
})

METER_HISTORY_STATUS_SCHEMA = vol.Schema({
    vol.Optional("job_id"): cv.string,
})

METER_HISTORY_PREVIEW_DATA_SCHEMA = vol.Schema({
    vol.Required("meter_source_id"): vol.Match(r"^meter_[0-9a-f]{20}$"),
    vol.Optional("job_id"): cv.string,
    vol.Optional("offset", default=0): vol.All(vol.Coerce(int), vol.Range(min=0)),
    vol.Optional("limit", default=250): vol.All(vol.Coerce(int), vol.Range(min=1, max=365)),
})


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
    # Serve packaged card assets directly; never copy code into /config/www
    # or create web-accessible files containing the private preview results.
    if not hass.data[DOMAIN].get("history_preview_card_registered"):
        await hass.http.async_register_static_paths([
            StaticPathConfig(
                "/engie/history-preview-card.js",
                str(Path(__file__).parent / "www" / "history-preview-card.js"),
                cache_headers=False,
            )
        ])
        hass.data[DOMAIN]["history_preview_card_registered"] = True
    return True


async def async_remove_config_entry_device(
    hass: HomeAssistant, config_entry: ConfigEntry, device_entry: DeviceEntry
) -> bool:
    """Permit removal of legacy contract devices only when fully retired.

    Device sources and the parent account must never be removable through
    this migration callback. An empty legacy device may be removed only
    after the user explicitly disables old contract-based sensors.
    """
    if bool(config_entry.options.get("legacy_contract_sensors", True)):
        return False
    if not any(
        domain == DOMAIN and identity.startswith("contract_")
        for domain, identity in device_entry.identifiers
    ):
        return False
    return not er.async_entries_for_device(er.async_get(hass), device_entry.id)


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
            if not bool(runtime["coordinator"].entry.options.get("legacy_contract_sensors", True)):
                raise HomeAssistantError(
                    "Legacy contract imports are disabled. Use "
                    "engie.import_meter_history for the new meter groups."
                )
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

    # New, source-scoped actions. These create background jobs and return
    # immediately, so "all available" does not block the HA action UI.
    def _find_history_coordinator(data: dict[str, Any]) -> EngieCoordinator:
        from homeassistant.helpers import entity_registry as entity_reg

        runtimes = [
            item for item in hass.data.get(DOMAIN, {}).values()
            if isinstance(item, dict) and "coordinator" in item
        ]
        if not runtimes:
            raise HomeAssistantError("No active ENGIE integration")
        entity_id = data.get("meter_entity")
        if entity_id:
            entity = entity_reg.async_get(hass).async_get(entity_id)
            if entity is None:
                raise HomeAssistantError("Selected meter sensor was not found")
            for item in runtimes:
                if entity.config_entry_id == item["coordinator"].entry.entry_id:
                    return item["coordinator"]
            raise HomeAssistantError("Selected meter does not belong to ENGIE")
        source_id = data.get("meter_source_id")
        if source_id:
            matching = [
                item["coordinator"] for item in runtimes
                if any(
                    source.source_id == source_id
                    for source in (
                        getattr(item["coordinator"].data, "meter_sources", None) or []
                    )
                )
            ]
            if len(matching) == 1:
                return matching[0]
            raise HomeAssistantError("Unknown or ambiguous ENGIE meter ID")
        if len(runtimes) != 1:
            raise HomeAssistantError(
                "More than one ENGIE integration: select a meter entity"
            )
        return runtimes[0]["coordinator"]

    async def _history_action(call: ServiceCall, *, preview: bool) -> dict[str, Any]:
        from .meter_history import start_history_job

        coordinator = _find_history_coordinator(dict(call.data))
        return start_history_job(
            hass,
            coordinator,
            meter_entity=call.data.get("meter_entity"),
            source_id=call.data.get("meter_source_id"),
            period=call.data.get("period", "365_days"),
            start_date=call.data.get("start_date"),
            end_date=call.data.get("end_date"),
            include_costs=call.data.get("include_costs", True),
            preview_only=preview,
        )

    async def _preview_meter_history(call: ServiceCall) -> dict[str, Any]:
        return await _history_action(call, preview=True)

    async def _import_meter_history(call: ServiceCall) -> dict[str, Any]:
        return await _history_action(call, preview=False)

    async def _meter_history_status(call: ServiceCall) -> dict[str, Any]:
        from .meter_history import get_history_job_status

        coordinator = _find_history_coordinator({})
        return get_history_job_status(hass, coordinator, call.data.get("job_id"))

    hass.services.async_register(
        DOMAIN, "preview_meter_history", _preview_meter_history,
        schema=METER_HISTORY_SCHEMA, **register_kwargs,
    )
    hass.services.async_register(
        DOMAIN, "import_meter_history", _import_meter_history,
        schema=METER_HISTORY_SCHEMA, **register_kwargs,
    )
    status_kwargs: dict[str, Any] = {}
    if SupportsResponse is not None:
        status_kwargs["supports_response"] = SupportsResponse.ONLY
    hass.services.async_register(
        DOMAIN, "meter_history_status", _meter_history_status,
        schema=METER_HISTORY_STATUS_SCHEMA, **status_kwargs,
    )

    async def _meter_history_preview_data(call: ServiceCall) -> dict[str, Any]:
        from .meter_history import get_history_preview_data

        coordinator = _find_history_coordinator(dict(call.data))
        return get_history_preview_data(
            hass, coordinator,
            source_id=call.data["meter_source_id"],
            job_id=call.data.get("job_id"),
            offset=call.data.get("offset", 0),
            limit=call.data.get("limit", 250),
        )

    hass.services.async_register(
        DOMAIN, "meter_history_preview_data", _meter_history_preview_data,
        schema=METER_HISTORY_PREVIEW_DATA_SCHEMA, **status_kwargs,
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

    async def _probe_gas_meter(call: ServiceCall) -> dict[str, Any]:
        # An in-memory SHA-256 comparison only: no PCE/PDL is passed as plain
        # text, stored, printed, or included in the service response.
        from .gas_meter_audit import audit_gas_meter_selection

        digest = call.data["pce_sha256"]
        for runtime in hass.data.get(DOMAIN, {}).values():
            if isinstance(runtime, dict) and "api" in runtime:
                api = runtime["api"]
                await api.async_ensure_session()
                return await api._run(audit_gas_meter_selection, api._client, digest)
        return {"error": "no_active_engie_session"}

    hass.services.async_register(
        DOMAIN,
        "probe_gas_meter",
        _probe_gas_meter,
        schema=vol.Schema({
            vol.Required("pce_sha256"): vol.Match(r"[0-9a-f]{64}")
        }),
        **probe_kwargs,
    )

    async def _dump_engie_api(_call: ServiceCall) -> dict[str, Any]:
        # Request bodies remain on the HA host; only redacted schemas returned.
        # This service is temporary and never imports or modifies statistics.
        from .private_api_dump import dump_engie_api_private

        digest = _call.data["pce_sha256"]
        for runtime in hass.data.get(DOMAIN, {}).values():
            if isinstance(runtime, dict) and "api" in runtime:
                api = runtime["api"]
                await api.async_ensure_session()
                return await api._run(
                    dump_engie_api_private,
                    api._client,
                    hass.config.path(),
                    digest,
                )
        return {"error": "no_active_engie_session"}

    hass.services.async_register(
        DOMAIN,
        "dump_private_api",
        _dump_engie_api,
        schema=vol.Schema({
            vol.Required("pce_sha256"): vol.Match(r"[0-9a-f]{64}")
        }),
        **probe_kwargs,
    )

    async def _probe_gas_across_accounts(call: ServiceCall) -> dict[str, Any]:
        from .gas_cross_account_probe import probe_gas_across_accounts

        digest = call.data["pce_sha256"]
        for runtime in hass.data.get(DOMAIN, {}).values():
            if isinstance(runtime, dict) and "api" in runtime:
                api = runtime["api"]
                await api.async_ensure_session()
                return await api._run(
                    probe_gas_across_accounts, api._client, digest
                )
        return {"error": "no_active_engie_session"}

    hass.services.async_register(
        DOMAIN,
        "probe_gas_across_accounts",
        _probe_gas_across_accounts,
        schema=vol.Schema({
            vol.Required("pce_sha256"): vol.Match(r"[0-9a-f]{64}")
        }),
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
        hass.services.async_remove(DOMAIN, "preview_meter_history")
        hass.services.async_remove(DOMAIN, "import_meter_history")
        hass.services.async_remove(DOMAIN, "meter_history_status")
        hass.services.async_remove(DOMAIN, "meter_history_preview_data")
        hass.services.async_remove(DOMAIN, "probe_gas")
        hass.services.async_remove(DOMAIN, "probe_gas_headers")
        hass.services.async_remove(DOMAIN, "probe_gas_meter")
        hass.services.async_remove(DOMAIN, "dump_private_api")
        hass.services.async_remove(DOMAIN, "probe_gas_across_accounts")
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    store = Store(hass, 1, f"engie_{entry.entry_id}")
    await store.async_remove()


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
