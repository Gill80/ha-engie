"""Read-only, privacy-preserving gas meter selection audit.

The user-provided PCE is *never* transmitted to the server in plaintext.
Caller sends its SHA-256 digest; meter references extracted in memory from
ENGIE responses are digested and compared without returning any identifier.

Avoid writing diagnostics to logs or to HA long-term statistics.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from typing import Any

import httpx

from .vendor.engie_particuliers.client import _energy_kind
from .vendor.engie_particuliers.exceptions import ApiError


# Names observed in Android 10.31 plus common ENGIE API variations.
# Deliberately exclude general account ids, access tokens and cookie fields.
_METER_REFERENCE_NAMES = frozenset({
    "pce", "numpce", "numeropce", "idpce", "pointcomptage",
    "pointdecomptage", "pointdecomptageetdestimation",
    "pdlgaz", "idpdlgaz", "numpdlgaz", "numeropdlgaz",
    "idpointcomptagegaz", "pointcomptagegaz",
})


def _reference_match_summary(data: Any, expected_sha256: str) -> dict[str, Any]:
    """Count PCE-looking fields, compare digest; never expose field values."""
    found = 0
    matching = 0
    nodes = 0
    stack: list[tuple[Any, int]] = [(data, 0)]
    while stack and nodes < 25000:
        item, depth = stack.pop()
        nodes += 1
        if depth > 14:
            continue
        if isinstance(item, dict):
            for key, value in item.items():
                label = re.sub(r"[^a-z0-9]", "", str(key).lower())
                if label in _METER_REFERENCE_NAMES:
                    if isinstance(value, (int, str)) and not isinstance(value, bool):
                        number = re.sub(r"[^0-9]", "", str(value))
                        if 10 <= len(number) <= 17:
                            found += 1
                            candidate = hashlib.sha256(number.encode("ascii")).hexdigest()
                            if hmac.compare_digest(candidate, expected_sha256):
                                matching += 1
                if isinstance(value, (dict, list)):
                    stack.append((value, depth + 1))
        elif isinstance(item, list):
            stack.extend((value, depth + 1) for value in item[:1000])
    return {
        "meter_reference_values_seen": found,
        "matching_reference_values": matching,
        "matches_supplied_gas_pce": matching > 0,
        "scan_truncated": bool(stack),
    }


def _safe_error(exc: Exception) -> dict[str, Any]:
    error = {"error_type": type(exc).__name__}
    if isinstance(exc, ApiError):
        error["http_status"] = exc.status_code
    return error


def audit_gas_meter_selection(client: Any, pce_sha256: str) -> dict[str, Any]:
    """Inspect the account listing and at most six account-detail requests.

    Always restore the outgoing cookie state. No gas/history write, no
    consumption import, no tokens/IDs/meter values returned.
    """
    if not re.fullmatch(r"[0-9a-f]{64}", pce_sha256):
        raise ValueError("pce_sha256 must be a lowercase SHA-256 digest")

    result: dict[str, Any] = {"audit_version": 1}
    try:
        account = client.get_account()
    except (ApiError, httpx.RequestError) as exc:
        result["account_error"] = _safe_error(exc)
        return result

    pairs = [
        (partner, cc)
        for partner in account.partners
        for cc in partner.comptes_contrat
    ]
    gas_pairs = [
        (partner, cc)
        for partner, cc in pairs
        if any(_energy_kind(contract) == "GAZ" for contract in cc.contrats)
    ]
    result["account_counts"] = {
        "business_partners": len(account.partners),
        "contract_accounts": len(pairs),
        "accounts_with_gas_contract": len(gas_pairs),
        "gas_contracts": sum(
            _energy_kind(contract) == "GAZ"
            for partner, cc in pairs
            for contract in cc.contrats
        ),
    }
    result["user_listing"] = _reference_match_summary(account.raw, pce_sha256)

    http = client._http_client()
    original_cookies = httpx.Cookies(http.cookies)
    reports: list[dict[str, Any]] = []
    try:
        for index, (partner, cc) in enumerate(pairs[:6]):
            gas_contracts = [
                contract for contract in cc.contrats
                if _energy_kind(contract) == "GAZ"
            ]
            entry: dict[str, Any] = {
                "account_index": index,
                "listed_gas_contracts": len(gas_contracts),
                "listed_electric_contracts": sum(
                    _energy_kind(contract) == "ELEC" for contract in cc.contrats
                ),
                "has_contract_number": bool(cc.numero_cc),
                "has_partner_reference": bool(partner.ref_bp),
            }
            if not partner.ref_bp or not cc.numero_cc:
                entry["skipped_missing_key"] = True
                reports.append(entry)
                continue
            client.select_account(partner.ref_bp, cc.numero_cc, partner.id_personne)
            for label, method in (
                ("releve_v2", client.get_releve),
                ("contract_details_v2", client.get_contracts),
            ):
                try:
                    data = method(partner.ref_bp, cc.numero_cc)
                    entry[label] = _reference_match_summary(data, pce_sha256)
                    entry[label]["http_status"] = 200
                except (ApiError, httpx.RequestError) as exc:
                    entry[label] = _safe_error(exc)
            reports.append(entry)
    finally:
        http.cookies = original_cookies

    result["accounts_checked"] = len(reports)
    result["accounts_truncated"] = len(pairs) > len(reports)
    result["account_checks"] = reports
    return result
