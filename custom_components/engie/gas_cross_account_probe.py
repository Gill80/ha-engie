"""Bounded read-only ENGIE gas probes for every account, including electric ones.

ENGIE may associate the active gas PCE with an account whose listeContrat
contains only electricity. Never presume a 'GAZ' listing is the correct
account for the gas consumption endpoints.

This diagnostic returns only HTTP status codes, response field names,
row counts and boolean PCE matches. Raw responses are kept in memory
during the call only, never returned, saved or logged. It does NOT
modify Home Assistant entities, statistics, credentials or configuration.
"""

from __future__ import annotations

from datetime import datetime
import re
from typing import Any

import httpx

from .private_api_dump import _match_pce, _read, _structure_paths
from .vendor.engie_particuliers.client import (
    _HISTORY_PATHS,
    _energy_kind,
    _range_ms,
)
from .vendor.engie_particuliers.exceptions import ApiError
from .vendor.engie_particuliers.models import PARIS


def _read_summary(record: dict[str, Any], digest: str) -> dict[str, Any]:
    """Keep schemas and counts, but never return response values."""
    body = record.get("body")
    summary: dict[str, Any] = {
        "http_status": record.get("http_status"),
        "body_type": record.get("response_body_type"),
        "pce_match": _match_pce(body, digest)["pce_match"],
    }
    if isinstance(body, dict):
        summary["top_level_field_names"] = [
            name for name in sorted(body)
            if re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,55}", name)
        ][:32]
        for key in (
            "historiquesGaz",
            "historiqueGaz",
            "previsionGaz",
            "data",
        ):
            if key in body:
                item = body[key]
                summary[key + "_type"] = type(item).__name__
                if isinstance(item, list):
                    summary[key + "_count"] = len(item)
        if record.get("http_status") == 200:
            shape = _structure_paths(body, max_paths=55)
            summary["schema"] = shape["paths"]
            summary["schema_truncated"] = shape["truncated"]
    elif isinstance(body, list):
        summary["root_list_count"] = len(body)
        if record.get("http_status") == 200:
            shape = _structure_paths(body, max_paths=55)
            summary["schema"] = shape["paths"]
            summary["schema_truncated"] = shape["truncated"]
    return summary


def probe_gas_across_accounts(client: Any, digest: str) -> dict[str, Any]:
    """Probe gas using both selected-account pairs; max 13 read-only GETs."""
    if not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise ValueError("Invalid lowercase SHA-256 PCE digest")

    result: dict[str, Any] = {"probe_version": 3}
    try:
        account = client.get_account()
    except (ApiError, httpx.RequestError) as exc:
        result["account_error_type"] = type(exc).__name__
        if isinstance(exc, ApiError):
            result["account_http_status"] = exc.status_code
        return result

    pairs = [
        (partner, cc)
        for partner in account.partners
        for cc in partner.comptes_contrat
    ]
    result["account_count"] = len(pairs)
    result["truncated_to_two"] = len(pairs) > 2

    # Fixed historical calendar interval with the user's confirmed mobile
    # October screenshot, plus a separate short rolling daily interval.
    oct1 = int(datetime(2026, 10, 1, tzinfo=PARIS).timestamp() * 1000)
    nov1 = int(datetime(2026, 11, 1, tzinfo=PARIS).timestamp() * 1000)
    rolling_start, rolling_end = _range_ms(days=14, granularity="day")

    http = client._http_client()
    original_cookies = httpx.Cookies(http.cookies)
    comparisons: list[dict[str, Any]] = []
    try:
        for index, (partner, cc) in enumerate(pairs[:2]):
            if not partner.ref_bp or not cc.numero_cc:
                comparisons.append({
                    "account_index": index, "skipped_missing_account_keys": True
                })
                continue
            client.select_account(partner.ref_bp, cc.numero_cc, partner.id_personne)
            basic = {"refBP": partner.ref_bp, "compteContrat": cc.numero_cc}
            postal = cc.adresse.cp if cc.adresse else ""
            report: dict[str, Any] = {
                "account_index": index,
                "listed_gas_contracts": sum(
                    _energy_kind(c) == "GAZ" for c in cc.contrats
                ),
                "listed_electric_contracts": sum(
                    _energy_kind(c) == "ELEC" for c in cc.contrats
                ),
                "postal_code_present": bool(postal),
            }
            meter = _read(
                client,
                "/dvmobile-ws/api/private/releve/recupererDonneesReleveV2",
                basic,
            )
            match = _match_pce(meter["body"], digest)
            report["meter_api_http_status"] = meter["http_status"]
            report["meter_pce_match"] = match["pce_match"]
            report["meter_matching_paths"] = match["matching_field_paths"]

            params = dict(basic)
            if postal:
                params["codePostal"] = postal
            calls: list[tuple[str, str, dict[str, Any]]] = [
                (
                    "gas_estimation",
                    "/dvmobile-ws/api/private/maConso/v2/estimationConsoGazMois",
                    basic,
                ),
                (
                    "gas_month_oct_communicant_true",
                    _HISTORY_PATHS[("GAZ", "month")],
                    {
                        **params,
                        "communicant": "true",
                        "startDate": oct1,
                        "endDate": nov1,
                    },
                ),
                (
                    "gas_month_oct_communicant_false",
                    _HISTORY_PATHS[("GAZ", "month")],
                    {
                        **params,
                        "communicant": "false",
                        "startDate": oct1,
                        "endDate": nov1,
                    },
                ),
                (
                    "gas_day_oct_calendar",
                    _HISTORY_PATHS[("GAZ", "day")],
                    {**params, "startDate": oct1, "endDate": nov1},
                ),
                (
                    "gas_day_rolling_14",
                    _HISTORY_PATHS[("GAZ", "day")],
                    {
                        **params,
                        "startDate": rolling_start,
                        "endDate": rolling_end,
                    },
                ),
            ]
            results: list[dict[str, Any]] = []
            for label, path, query in calls:
                raw = _read(client, path, query)
                results.append({
                    "request": label,
                    **_read_summary(raw, digest),
                })
            report["requests"] = results
            comparisons.append(report)
    finally:
        http.cookies = original_cookies
    result["comparisons"] = comparisons
    result["total_get_requests_cap"] = 13
    return result
