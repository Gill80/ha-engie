"""One-shot ENGIE API response dump kept on the Home Assistant host, privately.

The raw response bodies are NEVER returned via the service, logged, or uploaded
to GitHub. The file is created under /config/.engie-private-dumps (0700) and
opened with mode 0600. Auth credentials/tokens are masked even in that file.
The service response contains only anonymous response-schema paths and checks.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
from datetime import datetime, timezone
from typing import Any

import httpx

from .vendor.engie_particuliers.client import (
    _HISTORY_PATHS,
    _energy_kind,
    _range_ms,
)
from .vendor.engie_particuliers.exceptions import ApiError
from .vendor.engie_particuliers.models import PARIS


_SECRET_KEY = re.compile(
    r"(?:password|passwd|access.?token|refresh.?token|id.?token|"
    r"auth(?:orization)?|session.?token|cookie|secret|api.?key|"
    r"authorization|credential|bearer|client.?secret|"
    r"code.?verifier|state.?token)",
    re.IGNORECASE,
)
# Normalise scalar reference values, but never return them or their hashes.
_TARGET_DIGITS = re.compile(r"^[0-9]{10,17}$")
_QUANTITY_TYPES = ("int", "float")


def _scrub_auth_secrets(value: Any, depth: int = 0) -> Any:
    """Keep every API field and response value except credential-like fields."""
    if depth > 40:
        return "<depth_limit>"
    if isinstance(value, dict):
        return {
            str(k): (
                "<redacted_auth_secret>"
                if _SECRET_KEY.search(str(k))
                else _scrub_auth_secrets(v, depth + 1)
            )
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_scrub_auth_secrets(v, depth + 1) for v in value]
    return value


def _field_label(key: Any) -> str:
    """Do not disclose data-dependent dictionary keys (IDs, names, addresses)."""
    key = str(key)
    if re.search(r"\d{7,}", key) or len(key) > 100:
        return "<dynamic-key>"
    return key


def _structure_paths(value: Any, *, max_paths: int = 500) -> dict[str, Any]:
    """Discover schema-only field names and types with bounded output."""
    seen: set[tuple[str, str]] = set()
    stack = [(value, "$", 0)]
    node_count = 0
    while stack and len(seen) < max_paths and node_count < 12000:
        val, path, depth = stack.pop()
        node_count += 1
        kind = type(val).__name__
        if isinstance(val, dict):
            kind = "object"
        elif isinstance(val, list):
            kind = "array"
        elif val is None:
            kind = "null"
        seen.add((path, kind))
        if depth >= 12:
            continue
        if isinstance(val, dict):
            for key, child in val.items():
                stack.append((child, path + "." + _field_label(key), depth + 1))
        elif isinstance(val, list):
            if val:
                # Up to 3 items to detect varying schemas, not actual data.
                stack.extend((item, path + "[]", depth + 1) for item in val[:3])
    return {
        "paths": [
            {"path": path, "type": typ}
            for path, typ in sorted(seen)
        ],
        "truncated": bool(stack),
        "path_count": len(seen),
    }


def _match_pce(value: Any, digest: str) -> dict[str, Any]:
    """Match supplied PCE against *all* numeric scalar fields in each response."""
    candidates = 0
    hits: set[str] = set()
    nodes = 0
    stack: list[tuple[Any, str, int]] = [(value, "$", 0)]
    while stack and nodes < 30000:
        val, path, depth = stack.pop()
        nodes += 1
        if depth > 25:
            continue
        if isinstance(val, dict):
            stack.extend(
                (child, path + "." + _field_label(key), depth + 1)
                for key, child in val.items()
            )
        elif isinstance(val, list):
            stack.extend((item, path + "[]", depth + 1) for item in val[:2000])
        elif isinstance(val, (str, int)) and not isinstance(val, bool):
            normalized = re.sub(r"[\s\-.]", "", str(val))
            if _TARGET_DIGITS.fullmatch(normalized):
                candidates += 1
                candidate_sha = hashlib.sha256(normalized.encode()).hexdigest()
                if hmac.compare_digest(candidate_sha, digest):
                    hits.add(path)
    return {
        "numeric_references_seen": candidates,
        "pce_match": bool(hits),
        "matching_field_paths": sorted(hits),
        "truncated": bool(stack),
    }


def _read(client: Any, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Fetch a single API response, including 204/403 bodies when possible."""
    record: dict[str, Any] = {
        "method": "GET",
        "endpoint": path.rsplit("/", 1)[-1],
        "path": path,
        "query_parameter_names": sorted(params or {}),
    }
    try:
        response = client._api_request("GET", path, params=params)
        record["http_status"] = response.status_code
        record["content_type"] = (response.headers.get("content-type") or "").split(";", 1)[0]
        try:
            payload = response.json() if response.content else None
            record["response_body_type"] = "json" if response.content else "empty"
        except ValueError:
            payload = response.text
            record["response_body_type"] = "text"
        record["body"] = _scrub_auth_secrets(payload)
    except ApiError as err:
        record["http_status"] = err.status_code
        record["response_body_type"] = "error"
        record["body"] = _scrub_auth_secrets(err.body)
    except httpx.RequestError as err:
        record["http_status"] = None
        record["response_body_type"] = "transport_error"
        record["error_type"] = type(err).__name__
        record["body"] = None
    return record


def dump_engie_api_private(
    client: Any, config_path: str, pce_digest: str
) -> dict[str, Any]:
    """Store a bounded *complete body* dump on HA; return only a schema report.

    The request is read-only on ENGIE. Uses at most 12 requests when there are
    <= 4 accounts and <= 2 gas accounts. Does not persist query parameters.
    """
    if not re.fullmatch(r"[0-9a-f]{64}", pce_digest):
        raise ValueError("Expected lowercase SHA-256 of the user's gas PCE")

    result: dict[str, Any] = {"dump_version": 1}
    try:
        account = client.get_account()
    except (ApiError, httpx.RequestError) as exc:
        result["account_error_type"] = type(exc).__name__
        if isinstance(exc, ApiError):
            result["account_http_status"] = exc.status_code
        return result

    all_pairs = [
        (partner, cc)
        for partner in account.partners
        for cc in partner.comptes_contrat
    ]
    gas_accounts = [
        (partner, cc)
        for partner, cc in all_pairs
        if any(_energy_kind(contract) == "GAZ" for contract in cc.contrats)
    ]
    result["accounts_total"] = len(all_pairs)
    result["accounts_with_gas"] = len(gas_accounts)
    result["gas_contracts"] = sum(
        _energy_kind(c) == "GAZ"
        for _p, cc in all_pairs for c in cc.contrats
    )

    # Complete response body of getInfoUsers already retrieved through client.
    data: list[dict[str, Any]] = [
        {
            "endpoint": "getInfoUsers",
            "path": "/dvmobile-ws/api/private/espaceClient/v2/getInfoUsers",
            "method": "GET",
            "http_status": 200,
            "response_body_type": "json",
            "body": _scrub_auth_secrets(account.raw),
        }
    ]

    # Preserve the existing active HTTP cookie state.
    http = client._http_client()
    saved_cookies = httpx.Cookies(http.cookies)
    try:
        for idx, (partner, cc) in enumerate(all_pairs[:4]):
            client.select_account(partner.ref_bp, cc.numero_cc, partner.id_personne)
            for label, path in (
                ("getInfoContrats", "/dvmobile-ws/api/private/espaceClient/v2/getInfoContrats"),
                ("recupererDonneesReleveV2", "/dvmobile-ws/api/private/releve/recupererDonneesReleveV2"),
            ):
                record = _read(
                    client, path,
                    {"refBP": partner.ref_bp, "compteContrat": cc.numero_cc},
                )
                record["account_index"] = idx
                record["account_has_gas"] = any(
                    _energy_kind(c) == "GAZ" for c in cc.contrats
                )
                data.append(record)

        # Request a real calendar October 2026 month in Europe/Paris, plus
        # a normal rolling window, to distinguish aligned vs rolling queries.
        # Only run these for actual gas accounts in the retrieved listing.
        oct_start = datetime(2026, 10, 1, tzinfo=PARIS)
        nov_start = datetime(2026, 11, 1, tzinfo=PARIS)
        calendar_from = int(oct_start.timestamp() * 1000)
        calendar_to = int(nov_start.timestamp() * 1000)
        for partner, cc in gas_accounts[:2]:
            index = next(
                i for i, pair in enumerate(all_pairs)
                if pair[0] is partner and pair[1] is cc
            )
            client.select_account(partner.ref_bp, cc.numero_cc, partner.id_personne)
            basic = {"refBP": partner.ref_bp, "compteContrat": cc.numero_cc}
            postal = cc.adresse.cp if cc.adresse else ""
            estimate = _read(
                client,
                "/dvmobile-ws/api/private/maConso/v2/estimationConsoGazMois",
                basic,
            )
            estimate["account_index"] = index
            data.append(estimate)
            for grain in ("month", "day"):
                rolling_start, rolling_end = _range_ms(
                    days=60 if grain == "month" else 14, granularity=grain
                )
                path = _HISTORY_PATHS[("GAZ", grain)]
                for mode, start, end in (
                    ("rolling", rolling_start, rolling_end),
                    ("october_calendar_paris", calendar_from, calendar_to),
                ):
                    params = {
                        **basic,
                        "startDate": start,
                        "endDate": end,
                    }
                    if postal:
                        params["codePostal"] = postal
                    if grain == "month":
                        params["communicant"] = "true"
                    record = _read(client, path, params)
                    record["account_index"] = index
                    record["date_range_mode"] = mode
                    record["granularity"] = grain
                    data.append(record)
    finally:
        http.cookies = saved_cookies

    # Full raw JSON bodies are deliberately retained only on disk. This
    # summary is safe to return through Home Assistant MCP diagnostics.
    summaries: list[dict[str, Any]] = []
    for record in data:
        body = record["body"]
        summary = {
            "endpoint": record["endpoint"],
            "account_index": record.get("account_index"),
            "date_range_mode": record.get("date_range_mode"),
            "http_status": record.get("http_status"),
            "body_type": record["response_body_type"],
            "schema": _structure_paths(body),
            "pce": _match_pce(body, pce_digest),
        }
        summaries.append(summary)

    # Create this directory privately and never serve through /config/www.
    directory = Path(config_path) / ".engie-private-dumps"
    directory.mkdir(mode=0o700, parents=False, exist_ok=True)
    os.chmod(directory, 0o700)
    filename = "engie-api-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(4) + ".json"
    target = directory / filename
    document = {
        "notice": "PRIVATE. Contains personal ENGIE account/billing/meter data. Never upload to GitHub or post publicly.",
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "response_count": len(data),
        "responses": data,
    }
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(str(target), flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(document, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    result["local_private_path"] = str(target)
    result["file_bytes"] = target.stat().st_size
    result["file_mode"] = "0600"
    result["responses_saved"] = len(data)
    result["summaries"] = summaries
    result["pce_found_anywhere"] = any(
        s["pce"]["pce_match"] for s in summaries
    )
    return result
