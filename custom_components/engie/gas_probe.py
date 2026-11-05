"""Bounded one-shot gas endpoint probes, only included on the test branch.

No persistent state or response bodies are written. This module deliberately
does not log request URLs, account numbers, meter IDs or access tokens.
"""

from __future__ import annotations

from typing import Any

import httpx

from .vendor.engie_particuliers.client import (
    _HISTORY_PATHS,
    _energy_kind,
    _range_ms,
    _releve_contract,
)
from .vendor.engie_particuliers.exceptions import ApiError


def _error_summary(exc: Exception) -> dict[str, Any]:
    result: dict[str, Any] = {"error_type": type(exc).__name__}
    if isinstance(exc, ApiError):
        result["http_status"] = exc.status_code
    return result


def probe_gas_readonly(client: Any) -> dict[str, Any]:
    """Run 4 carefully bounded history requests for the first gas account."""
    report: dict[str, Any] = {"probe_version": 1}
    try:
        account = client.get_account()
    except (ApiError, httpx.RequestError) as exc:
        report["account_error"] = _error_summary(exc)
        return report

    matches = [
        (partner, cc)
        for partner in account.partners
        for cc in partner.comptes_contrat
        if any(_energy_kind(contract) == "GAZ" for contract in cc.contrats)
    ]
    report["gas_account_count"] = len(matches)
    if not matches:
        return report

    partner, cc = matches[0]
    postal = cc.adresse.cp if cc.adresse else ""
    report["account_context"] = {
        "has_partner_ref": bool(partner.ref_bp),
        "has_account_number": bool(cc.numero_cc),
        "has_person_id": bool(partner.id_personne),
        "has_postal_code": bool(postal),
        "contracts_on_account": len(cc.contrats),
    }
    client.select_account(partner.ref_bp, cc.numero_cc, partner.id_personne)

    communicant = True
    try:
        releve = client.get_releve(partner.ref_bp, cc.numero_cc)
        meter = _releve_contract(releve, "GAZ")
        report["account_context"]["gas_releve_present"] = bool(meter)
        report["account_context"]["communicant_flag_present"] = (
            "compteurCommuniquant" in meter
        )
        if "compteurCommuniquant" in meter:
            communicant = bool(meter["compteurCommuniquant"])
            report["account_context"]["communicant"] = communicant
    except (ApiError, httpx.RequestError) as exc:
        report["releve_error"] = _error_summary(exc)

    # Compare the contract details route declared in Android 10.31.0.
    try:
        response = client._api_request(
            "GET",
            "/dvmobile-ws/api/private/espaceClient/v2/getInfoContrats",
            params={"refBP": partner.ref_bp, "compteContrat": cc.numero_cc},
        )
        report["contract_v2_http_status"] = response.status_code
    except (ApiError, httpx.RequestError) as exc:
        report["contract_v2_error"] = _error_summary(exc)

    probes: list[dict[str, Any]] = []
    # Requests in each pair have exactly the same URL and query parameters.
    # Only Cache-Control is varied. At most four history calls.
    for grain, days in (("month", 60), ("day", 14)):
        start, end = _range_ms(days=days, granularity=grain)
        path = _HISTORY_PATHS[("GAZ", grain)]
        endpoint = path.rsplit("/", 1)[-1]
        params: dict[str, Any] = {
            "refBP": partner.ref_bp,
            "compteContrat": cc.numero_cc,
            "startDate": start,
            "endDate": end,
        }
        if postal:
            params["codePostal"] = postal
        if grain == "month":
            params["communicant"] = "true" if communicant else "false"
        for no_cache in (False, True):
            attempt: dict[str, Any] = {
                "endpoint": endpoint,
                "days": days,
                "cache_control_no_cache": no_cache,
            }
            headers = client._http_client().headers
            previous_cache = headers.get("Cache-Control")
            before = len(client.gas_api_diagnostics.get(endpoint, []))
            try:
                if no_cache:
                    headers["Cache-Control"] = "no-cache"
                else:
                    headers.pop("Cache-Control", None)
                try:
                    response = client._api_request("GET", path, params=params)
                    attempt["http_status"] = response.status_code
                except (ApiError, httpx.RequestError) as exc:
                    attempt.update(_error_summary(exc))
            finally:
                if previous_cache is None:
                    headers.pop("Cache-Control", None)
                else:
                    headers["Cache-Control"] = previous_cache
            history = client.gas_api_diagnostics.get(endpoint, [])
            if len(history) > before:
                summary = history[-1]
                for key in (
                    "response_type", "history_field_type",
                    "history_row_count", "recognized_fields",
                ):
                    if key in summary:
                        attempt[key] = summary[key]
            probes.append(attempt)
    report["probes"] = probes
    return report
