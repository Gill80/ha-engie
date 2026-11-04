"""On-demand ENGIE history jobs for independently identified physical meters.

Only the integration's own hashed engie:meter_* statistics are written.
LiXee, Linky, Zigbee, ZiPulses, existing ENGIE contract statistics and
Home Assistant Energy preferences are explicitly out of scope.

All history operations are started by a user action, never at polling time.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
import re
import secrets
from typing import Any, Callable

from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store

from .const import DOMAIN, STREAM_COST, STREAM_ENERGY
from .meter_history_core import (
    cumulative_statistics,
    local_midnight_ms,
    normalize_daily_points,
    plan_period,
    reconcile_daily,
    windows_reverse,
)
from .statistics import _metadata, statistic_id

_LOGGER = logging.getLogger(__name__)
_SAFE_ID = re.compile(r"^meter_[0-9a-f]{20}$")
_PERIOD_LABEL = {
    "90_days": "90 jours",
    "365_days": "12 mois",
    "all_available": "Tout l'historique (10 ans maximum)",
    "custom": "Période personnalisée",
}


def _runtime(hass: HomeAssistant, coordinator: Any) -> dict[str, Any]:
    runtime = hass.data.get(DOMAIN, {}).get(coordinator.entry.entry_id)
    if not isinstance(runtime, dict) or runtime.get("coordinator") is not coordinator:
        raise HomeAssistantError("ENGIE configuration not available")
    return runtime


def _selected_sources(
    hass: HomeAssistant,
    coordinator: Any,
    *,
    meter_entity: str | None = None,
    source_id: str | None = None,
) -> list[Any]:
    """Whitelist IDs against live source inventory before any GET or write."""
    snapshot = coordinator.data
    catalog = list(getattr(snapshot, "meter_sources", None) or [])
    catalog = [
        source for source in catalog
        if _SAFE_ID.fullmatch(str(source.source_id))
    ]
    if source_id and meter_entity:
        raise HomeAssistantError("Choose a meter entity or a source ID, not both")
    if meter_entity:
        registration = er.async_get(hass).async_get(meter_entity)
        if registration is None or registration.config_entry_id != coordinator.entry.entry_id:
            raise HomeAssistantError("Selected entity does not belong to ENGIE")
        source_id = next(
            (
                source.source_id
                for source in catalog
                if registration.unique_id.startswith(
                    f"{coordinator.entry.entry_id}_{source.source_id}_"
                )
            ),
            None,
        )
        if not source_id:
            raise HomeAssistantError("Selected entity is not an ENGIE meter sensor")
    if source_id:
        matches = [source for source in catalog if source.source_id == source_id]
        if len(matches) != 1:
            raise HomeAssistantError("Unknown ENGIE meter source")
        return matches
    return catalog


async def _notify(hass: HomeAssistant, text: str, title: str) -> None:
    """Use the built-in HA notification UI, with masked meter labels only."""
    if hass.services.has_service("persistent_notification", "create"):
        await hass.services.async_call(
            "persistent_notification", "create",
            {"title": title, "message": text},
            blocking=False,
        )


async def _collect(
    coordinator: Any,
    sources: list[Any],
    *,
    period: str,
    start_date: str | None,
    end_date: str | None,
    include_costs: bool,
    previous: dict[str, Any],
    update_progress: Callable[[int, int], None],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    first, exclusive_end = plan_period(
        period, start_date=start_date, end_date=end_date
    )
    windows = windows_reverse(first, exclusive_end)
    total_steps = len(windows) * len(sources)
    done = 0
    proposals: list[dict[str, Any]] = []
    updated_sources = dict(previous.get("sources") or {})

    for source in sources:
        existing = dict(updated_sources.get(source.source_id) or {})
        fetched: dict[str, dict[str, float | None]] = {}
        for begin, end in windows:
            # All timestamps use local Paris midnight, like the Android app.
            # A GET is always scoped to the physical meter's OWN contract CC.
            points = await coordinator.api.async_get_meter_source_points(
                source.source_id,
                start_ms=local_midnight_ms(begin),
                end_ms=local_midnight_ms(end),
            )
            new_rows = normalize_daily_points(
                points, first=begin, exclusive_end=end
            )
            fetched.update(new_rows)
            done += 1
            update_progress(done, total_steps)

        if not include_costs:
            # Preserve existing historical costs but don't import any new
            # cost values when the user asks for electricity/gas only.
            fetched = {
                day: {**row, "cost_ttc": None}
                for day, row in fetched.items()
            }
        merged, changes = reconcile_daily(existing, fetched)
        updated_sources[source.source_id] = merged
        present_costs = sum(
            1 for row in fetched.values() if row["cost_ttc"] is not None
        )
        proposals.append({
            "_source": source,
            "_merged": merged,
            "result": {
                "meter": source.display_name,
                "energy": source.energy,
                "source_id": source.source_id,
                "requested_first": first.isoformat(),
                "requested_last": (exclusive_end - timedelta(days=1)).isoformat(),
                "windows": len(windows),
                "daily_readings": len(fetched),
                "daily_costs": present_costs,
                "first_available": min(fetched) if fetched else None,
                "last_available": max(fetched) if fetched else None,
                **changes,
                "statistic_energy": statistic_id(source.source_id, STREAM_ENERGY),
                "statistic_cost": (
                    statistic_id(source.source_id, STREAM_COST)
                    if include_costs else None
                ),
            },
        })
    return proposals, {"sources": updated_sources}


async def _execute(
    hass: HomeAssistant,
    coordinator: Any,
    *,
    sources: list[Any],
    period: str,
    start_date: str | None,
    end_date: str | None,
    include_costs: bool,
    preview_only: bool,
    update_progress: Callable[[int, int], None],
) -> dict[str, Any]:
    store = Store(
        hass, 1, f"engie_meter_history_{coordinator.entry.entry_id}"
    )
    previous = await store.async_load() or {}
    proposals, data = await _collect(
        coordinator, sources,
        period=period,
        start_date=start_date,
        end_date=end_date,
        include_costs=include_costs,
        previous=previous,
        update_progress=update_progress,
    )
    results = [p["result"] for p in proposals]
    changed = sum(p["result"]["new_days"] + p["result"]["corrected_days"] for p in proposals)
    report: dict[str, Any] = {
        "mode": "preview" if preview_only else "import",
        "period": period,
        "meter_count": len(sources),
        "new_or_corrected_days": changed,
        "meters": results,
        "statistics_scope": "engie:meter_* only",
    }
    if preview_only or changed == 0:
        report["statistics_queued"] = 0
        report["storage_changed"] = False
        return report

    # Finish collecting all meters before writing anything: an API failure
    # does not leave partial imports for the other selected meters.
    queued = 0
    for proposal in proposals:
        source = proposal["_source"]
        if not proposal["_merged"]:
            continue
        for stream in (
            (STREAM_ENERGY, STREAM_COST)
            if include_costs else (STREAM_ENERGY,)
        ):
            statistics = cumulative_statistics(
                proposal["_merged"], stream=stream
            )
            if not statistics:
                continue
            # _metadata uses source='engie' and engie:<hashed ID> only.
            async_add_external_statistics(
                hass,
                _metadata(source.source_id, stream, source.display_name),
                statistics,
            )
            queued += len(statistics)
    # A dedicated private store provides idempotent reimport and recomputes
    # later sums after ENGIE corrects a historical reading.
    await store.async_save(data)
    report["statistics_queued"] = queued
    report["storage_changed"] = True
    return report


def start_history_job(
    hass: HomeAssistant,
    coordinator: Any,
    *,
    meter_entity: str | None = None,
    source_id: str | None = None,
    period: str = "365_days",
    start_date: str | None = None,
    end_date: str | None = None,
    include_costs: bool = True,
    preview_only: bool = True,
) -> dict[str, Any]:
    """Start a non-blocking one-shot job; results are available via status."""
    # Validate the dates and membership *before* scheduling anything.
    first, end = plan_period(period, start_date=start_date, end_date=end_date)
    sources = _selected_sources(
        hass, coordinator, meter_entity=meter_entity, source_id=source_id
    )
    if not sources:
        raise HomeAssistantError("No ENGIE meters discovered")
    runtime = _runtime(hass, coordinator)
    jobs: dict[str, Any] = runtime.setdefault("meter_history_jobs", {})
    if any(j.get("status") == "running" for j in jobs.values()):
        raise HomeAssistantError("An ENGIE history job is already running")

    job_id = secrets.token_hex(6)
    job: dict[str, Any] = {
        "id": job_id,
        "status": "running",
        "preview_only": preview_only,
        "period": period,
        "period_label": _PERIOD_LABEL[period],
        "meters": [source.display_name for source in sources],
        "window_count": len(windows_reverse(first, end)) * len(sources),
        "windows_completed": 0,
        "result": None,
    }
    jobs[job_id] = job
    # Bounded diagnostics: keep at most 8 job records in memory.
    for old_id in list(jobs)[:-8]:
        if jobs[old_id].get("status") != "running":
            jobs.pop(old_id, None)

    def progress(done: int, total: int) -> None:
        job["windows_completed"] = done
        job["window_count"] = total

    async def work() -> None:
        try:
            result = await _execute(
                hass, coordinator,
                sources=sources,
                period=period,
                start_date=start_date,
                end_date=end_date,
                include_costs=include_costs,
                preview_only=preview_only,
                update_progress=progress,
            )
        except Exception as err:  # noqa: BLE001 - background task must report status
            job["status"] = "failed"
            job["error_type"] = type(err).__name__
            # The exception body may contain account information or tokens.
            # Only expose a validated HTTP status and a technical error type.
            status_code = getattr(err, "status_code", None)
            if type(status_code) is int and 100 <= status_code <= 599:
                job["http_status"] = status_code
                error_label = f"HTTP {status_code} ({type(err).__name__})"
            else:
                error_label = type(err).__name__
            _LOGGER.warning(
                "ENGIE history job %s failed: %s", job_id, error_label
            )
            await _notify(
                hass,
                "La récupération a échoué. Aucun historique n'a été importé "
                "si l'erreur s'est produite pendant le téléchargement. "
                f"Erreur : {error_label}. "
                "Consultez l'action engie.meter_history_status pour le suivi.",
                "ENGIE · Import d'historique",
            )
            return
        job["status"] = "completed"
        job["result"] = result
        days = result["new_or_corrected_days"]
        title = (
            "ENGIE · Prévisualisation d'historique"
            if preview_only else "ENGIE · Historique importé"
        )
        await _notify(
            hass,
            f"Compteur(s) : {', '.join(job['meters'])}\n\n"
            f"Période : {job['period_label']}\n\n"
            f"Journées nouvelles ou corrigées : **{days}**.\n\n"
            + (
                "Aucune statistique modifiée : utilisez le bouton Importer "
                "pour confirmer."
                if preview_only
                else "Seules les statistiques propres à ENGIE ont été mises en file "
                "d'attente pour le Recorder."
            ),
            title,
        )

    hass.async_create_task(work(), name=f"engie_meter_history_{job_id}")
    return {
        "job_id": job_id,
        "status": "started",
        "preview_only": preview_only,
        "period": period,
        "meter_count": len(sources),
        "windows_planned": job["window_count"],
    }


def get_history_job_status(
    hass: HomeAssistant, coordinator: Any, job_id: str | None = None
) -> dict[str, Any]:
    """Read the latest or specified job status, no private values."""
    jobs = _runtime(hass, coordinator).get("meter_history_jobs", {})
    if not jobs:
        return {"status": "no_job"}
    if not job_id:
        job_id = next(reversed(jobs))
    job = jobs.get(job_id)
    if job is None:
        return {"status": "not_found"}
    return dict(job)
