"""Second bounded read-only gas probe: compare Android 10.28 vs 10.31 User-Agent.

Never return or log credentials, cookie values, contract numbers, paths with
query parameters, response bodies, or individual consumption data.
"""

from __future__ import annotations

from typing import Any

import httpx

from .vendor.engie_particuliers.client import (
    API_HOST,
    USER_AGENT,
    _HISTORY_PATHS,
    _energy_kind,
    _range_ms,
    _releve_contract,
)
from .vendor.engie_particuliers.exceptions import ApiError


_APP_1031_UA = USER_AGENT.replace("Mozilla/10.28.0", "Mozilla/10.31.0", 1)
_GAS_ESTIMATION_PATH = "/dvmobile-ws/api/private/maConso/v2/estimationConsoGazMois"
_COOKIE_NAMES = {
    "access_token", "social_token", "ClientIDCookie",
    "Cel_Cc", "Cel_Bp", "idPersonne",
    "secureAPIToken", "secureAPI_SEC_NUM_CC",
    "secureAPI_SEC_REF_BP", "secureAPI_SEC_ID_CONTRAT",
    "secureAPI_SEC_ID_HABITUDE_CONSOMMATION",
    "secureAPI_SEC_ID_FOYER", "secureAPI_SEC_ID_LOGEMENT",
}


def _error_summary(exc: Exception) -> dict[str, Any]:
    result: dict[str, Any] = {"error_type": type(exc).__name__}
    if isinstance(exc, ApiError):
        result["http_status"] = exc.status_code
    return result


def probe_gas_mobile_headers_readonly(client: Any) -> dict[str, Any]:
    """Six bounded GETs, switching ONLY the user-agent release number."""
    result: dict[str, Any] = {
        "probe_version": 2,
        "versions_compared": ["10.28.0", "10.31.0"],
    }
    try:
        account = client.get_account()
    except (ApiError, httpx.RequestError) as exc:
        result["account_error"] = _error_summary(exc)
        return result

    candidates = [
        (partner, cc)
        for partner in account.partners
        for cc in partner.comptes_contrat
        if any(_energy_kind(contract) == "GAZ" for contract in cc.contrats)
    ]
    result["gas_account_count"] = len(candidates)
    if not candidates:
        return result
    partner, cc = candidates[0]
    postal = cc.adresse.cp if cc.adresse else ""
    client.select_account(partner.ref_bp, cc.numero_cc, partner.id_personne)

    communicant = True
    try:
        meter = _releve_contract(
            client.get_releve(partner.ref_bp, cc.numero_cc), "GAZ"
        )
        if "compteurCommuniquant" in meter:
            communicant = bool(meter["compteurCommuniquant"])
    except (ApiError, httpx.RequestError) as exc:
        result["meter_error"] = _error_summary(exc)

    jar_names = {
        cookie.name
        for cookie in client._http_client().cookies.jar
        if cookie.domain.lstrip(".") == API_HOST
    }
    # Boolean flags only, for explicitly known cookie names.
    result["cookie_presence"] = {
        name: name in jar_names
        for name in sorted(_COOKIE_NAMES)
    }
    result["postal_code_present"] = bool(postal)
    result["communicant"] = communicant

    start_month, end_month = _range_ms(days=60, granularity="month")
    start_day, end_day = _range_ms(days=14, granularity="day")
    requests = [
        (
            "estimationConsoGazMois",
            _GAS_ESTIMATION_PATH,
            {"refBP": partner.ref_bp, "compteContrat": cc.numero_cc},
        ),
        (
            "histoGazMois",
            _HISTORY_PATHS[("GAZ", "month")],
            {
                "refBP": partner.ref_bp,
                "compteContrat": cc.numero_cc,
                "codePostal": postal,
                "startDate": start_month,
                "endDate": end_month,
                "communicant": "true" if communicant else "false",
            },
        ),
        (
            "histoGazJours",
            _HISTORY_PATHS[("GAZ", "day")],
            {
                "refBP": partner.ref_bp,
                "compteContrat": cc.numero_cc,
                "codePostal": postal,
                "startDate": start_day,
                "endDate": end_day,
            },
        ),
    ]
    # Preserve the exact parameter shape used by the original client.
    for endpoint, _path, params in requests:
        if not postal and endpoint != "estimationConsoGazMois":
            params.pop("codePostal")

    reports: list[dict[str, Any]] = []
    for endpoint, path, params in requests:
        for variant, user_agent in (
            ("library_10_28", USER_AGENT),
            ("android_10_31", _APP_1031_UA),
        ):
            item: dict[str, Any] = {
                "endpoint": endpoint,
                "user_agent_variant": variant,
            }
            before = len(client.gas_api_diagnostics.get(endpoint, []))
            try:
                response = client._api_request(
                    "GET", path, params=params, user_agent=user_agent
                )
                item["http_status"] = response.status_code
            except (ApiError, httpx.RequestError) as exc:
                item.update(_error_summary(exc))
            history = client.gas_api_diagnostics.get(endpoint, [])
            if len(history) > before:
                summary = history[-1]
                for field in (
                    "response_type", "history_field_type",
                    "history_row_count", "recognized_fields",
                    "data_field_type", "field_count",
                ):
                    if field in summary:
                        item[field] = summary[field]
            reports.append(item)

    result["responses"] = reports
    return result
