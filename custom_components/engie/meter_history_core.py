"""Pure, offline-safe planning and reconciliation for ENGIE per-meter histories.

No Home Assistant APIs and no network I/O in this module. Each stored entry
is one *completed* French civil day, keyed to an opaque meter-source ID.
The data here is not a Linky, ZiPulses or other integration's counter.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
import math
from typing import Any, Iterable

from .vendor.engie_particuliers.models import PARIS, ConsumptionPoint, parse_engie_datetime


HISTORY_CHUNK_DAYS = 90
HISTORY_MAX_DAYS = 3653  # bounded search up to roughly ten years
PERIODS = ("90_days", "365_days", "all_available", "custom")


def plan_period(
    period: str,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    today: date | None = None,
) -> tuple[date, date]:
    """Return [first_day, exclusive_end) for completed Paris calendar days."""
    now = today or datetime.now(PARIS).date()
    last_complete_exclusive = now
    if period == "90_days":
        return now - timedelta(days=90), last_complete_exclusive
    if period == "365_days":
        return now - timedelta(days=365), last_complete_exclusive
    if period == "all_available":
        return now - timedelta(days=HISTORY_MAX_DAYS), last_complete_exclusive
    if period != "custom":
        raise ValueError("Unsupported period")
    if not start_date or not end_date:
        raise ValueError("Custom period requires start_date and end_date")
    try:
        first, last = date.fromisoformat(start_date), date.fromisoformat(end_date)
    except ValueError as err:
        raise ValueError("Dates must be YYYY-MM-DD") from err
    exclusive_end = last + timedelta(days=1)
    if first >= exclusive_end:
        raise ValueError("The start date must not be after the end date")
    if exclusive_end > last_complete_exclusive:
        raise ValueError("Only completed days may be imported (ending yesterday)")
    if first < now - timedelta(days=HISTORY_MAX_DAYS):
        raise ValueError("Requested period exceeds the ten-year safety limit")
    return first, exclusive_end


def windows_reverse(first: date, exclusive_end: date) -> list[tuple[date, date]]:
    """Walk backward in non-overlapping, <=90-civil-day intervals."""
    if first >= exclusive_end:
        raise ValueError("Empty interval")
    items: list[tuple[date, date]] = []
    end = exclusive_end
    while end > first:
        begin = max(first, end - timedelta(days=HISTORY_CHUNK_DAYS))
        items.append((begin, end))
        end = begin
    return items


def local_midnight_ms(day: date) -> int:
    """ENGIE Android uses Europe/Paris local-midnight Unix milliseconds."""
    return int(datetime.combine(day, time.min, tzinfo=PARIS).timestamp() * 1000)


def normalize_daily_points(
    points: Iterable[ConsumptionPoint],
    *,
    first: date,
    exclusive_end: date,
) -> dict[str, dict[str, float | None]]:
    """Select complete valid daily readings, one per actual French civil day."""
    rows: dict[str, dict[str, float | None]] = {}
    for point in points:
        if point.granularity != "day" or point.partial or point.kwh is None:
            continue
        parsed = parse_engie_datetime(point.start or point.end)
        if parsed is None:
            continue
        day = parsed.astimezone(PARIS).date()
        if not (first <= day < exclusive_end):
            continue
        try:
            kwh = float(point.kwh)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(kwh) or kwh < 0:
            continue
        cost: float | None = None
        if point.cost_ttc is not None:
            try:
                val = float(point.cost_ttc)
            except (TypeError, ValueError):
                val = float("nan")
            if math.isfinite(val):
                cost = val
        rows[day.isoformat()] = {"kwh": kwh, "cost_ttc": cost}
    return rows


def reconcile_daily(
    existing: dict[str, Any],
    fresh: dict[str, dict[str, float | None]],
) -> tuple[dict[str, dict[str, float | None]], dict[str, int]]:
    """Upsert by civil date, retaining previously known cost when omitted.

    Re-importing the same days is idempotent; correcting old values causes
    cumulative sums to be recomputed for *all later days*, not just the range.
    """
    merged: dict[str, dict[str, float | None]] = {
        day: {
            "kwh": float(row["kwh"]),
            "cost_ttc": (
                float(row["cost_ttc"]) if row.get("cost_ttc") is not None else None
            ),
        }
        for day, row in existing.items()
        if isinstance(row, dict) and row.get("kwh") is not None
    }
    new, changed, unchanged = 0, 0, 0
    for day, row in fresh.items():
        old = merged.get(day)
        cost = row.get("cost_ttc")
        if old is not None and cost is None:
            cost = old.get("cost_ttc")
        effective = {"kwh": row["kwh"], "cost_ttc": cost}
        if old is None:
            new += 1
        elif old != effective:
            changed += 1
        else:
            unchanged += 1
        merged[day] = effective
    return merged, {
        "new_days": new,
        "corrected_days": changed,
        "unchanged_days": unchanged,
        "total_days": len(merged),
    }


def cumulative_statistics(
    rows: dict[str, dict[str, float | None]], *, stream: str
) -> list[dict[str, Any]]:
    """Compute stable full-timeline Recorder external-statistics rows.

    Independent statistic IDs must be chosen by the caller. Never mix the
    historical kWh readings with physical cumulative PDL/PCE meter indexes.
    """
    if stream not in {"energy", "cost"}:
        raise ValueError("Invalid history stream")
    cumulative = 0.0
    result: list[dict[str, Any]] = []
    for day in sorted(rows):
        value = rows[day].get("kwh" if stream == "energy" else "cost_ttc")
        if value is None:
            continue
        delta = float(value)
        if not math.isfinite(delta):
            continue
        start = datetime.combine(
            date.fromisoformat(day), time.min, tzinfo=PARIS
        ).astimezone(timezone.utc)
        cumulative += delta
        result.append({
            "start": start,
            "state": delta,
            "sum": round(cumulative, 6),
        })
    return result
