"""Diagnostics for ENGIE Particuliers."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator import EngieCoordinator
from .sensor import _snapshot

TO_REDACT = {
    CONF_USERNAME,
    CONF_PASSWORD,
    "access_token",
    "refresh_token",
    "id_token",
    "email",
    "nom",
    "prenom",
    "ref_bp",
    "id_personne",
    "numero_cc",
    "contract_id",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    runtime = hass.data.get(DOMAIN, {}).get(entry.entry_id) or {}
    coordinator: EngieCoordinator | None = runtime.get("coordinator")
    snapshot = _snapshot(coordinator) if coordinator is not None else None
    contracts: list[dict[str, Any]] = []
    for item in (snapshot.contracts if snapshot else []) or []:
        price = item.price
        last = item.last_day
        contracts.append(
            {
                "energy": price.energy,
                "libelle": price.libelle,
                "type_comptage": price.type_comptage,
                "has_price_ttc": price.price_kwh_ttc is not None,
                "cadran_count": len(price.cadrans or []),
                "source": price.source,
                "recent_days": len(item.recent_days or []),
                "last_day_has_kwh": last.kwh is not None if last else False,
            }
        )
    billing: list[dict[str, Any]] = []
    for item in (snapshot.billing if snapshot else []) or []:
        billing.append(
            {
                "has_solde": item.balance.solde is not None,
                "has_next_invoice": bool(item.balance.next_invoice_date),
                "invoice_count": item.invoice_count,
                "energie_coupee": item.balance.energie_coupee,
                "recouvrement": item.balance.recouvrement,
            }
        )
    meter_sources: list[dict[str, Any]] = []
    for source in (snapshot.meter_sources if snapshot else []) or []:
        meter_sources.append({
            "energy": source.energy,
            "declared_contract": source.declared_contract,
            "matches_selected_gas_pce": source.matched_selected_pce,
            "has_meter_reference_suffix": bool(source.meter_suffix),
            "recent_days": len(source.recent_days),
            "has_price_ttc": bool(
                source.price and source.price.price_kwh_ttc is not None
            ),
        })
    return {
        "entry": {
            "title": "**REDACTED**",
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
        },
        "runtime": {
            "authenticated": bool(
                coordinator.api.authenticated if coordinator is not None else False
            ),
            "last_update_success": (
                coordinator.last_update_success if coordinator is not None else None
            ),
            "contract_count": len(contracts),
            "billing_count": len(billing),
        },
        "contracts": contracts,
        "billing": billing,
        "meter_sources": meter_sources,
        # The library only supplies allowlisted schema keys and transport
        # metadata; it never contains account IDs, tokens, URLs, or values.
        "gas_api": (
            coordinator.api.gas_api_diagnostics if coordinator is not None else {}
        ),
    }
