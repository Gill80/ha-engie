"""Import ENGIE daily consumption into Home Assistant long-term statistics."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from functools import partial
from typing import TYPE_CHECKING, Any

from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.components.recorder.tasks import ClearStatisticsTask
from homeassistant.const import UnitOfEnergy
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.recorder import get_instance
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import EnergyConverter

from .const import (
    CLEAR_STATISTICS_TIMEOUT_SECONDS,
    DOMAIN,
    HISTORY_BACKFILL_DAYS,
    HISTORY_HEAL_DAYS,
    STREAM_COST,
    STREAM_ENERGY,
)

if TYPE_CHECKING:
    from engie_particuliers.models import AccountSnapshot, ConsumptionPoint

    from .coordinator import EngieCoordinator

_LOGGER = logging.getLogger(__name__)

_CONTRACT_SLUG = re.compile(r"[^a-zA-Z0-9_]+")


def statistic_id(contract_id: str, stream: str) -> str:
    slug = _CONTRACT_SLUG.sub("_", contract_id).strip("_") or "contract"
    return f"{DOMAIN}:{slug}_{stream}"


def _metadata(contract_id: str, stream: str, name: str) -> StatisticMetaData:
    if stream == STREAM_COST:
        unit = "EUR"
        unit_class = None
        label = f"Historical cost - {name}"
    else:
        unit = UnitOfEnergy.KILO_WATT_HOUR
        unit_class = getattr(EnergyConverter, "UNIT_CLASS", None)
        label = f"Historical consumption - {name}"
    kwargs: dict[str, Any] = {
        "has_sum": True,
        "name": label,
        "source": DOMAIN,
        "statistic_id": statistic_id(contract_id, stream),
        "unit_of_measurement": unit,
    }
    # Home Assistant 2026.11 requires the key even for monetary statistics,
    # for which there is no unit-conversion class (None is intentional).
    kwargs["unit_class"] = unit_class
    try:
        from homeassistant.components.recorder.models import StatisticMeanType

        return StatisticMetaData(mean_type=StatisticMeanType.NONE, **kwargs)
    except (ImportError, TypeError):
        kwargs["has_mean"] = False
        return StatisticMetaData(**kwargs)


def _point_start(point: ConsumptionPoint) -> datetime | None:
    raw = point.start or point.end
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    local = dt_util.as_local(parsed)
    return dt_util.as_utc(local.replace(hour=0, minute=0, second=0, microsecond=0))


def _usable_points(points: list[ConsumptionPoint]) -> list[tuple[datetime, ConsumptionPoint]]:
    rows: list[tuple[datetime, ConsumptionPoint]] = []
    seen: set[datetime] = set()
    for point in points:
        if point.kwh is None or getattr(point, "partial", False):
            continue
        start = _point_start(point)
        if start is None or start in seen:
            continue
        seen.add(start)
        rows.append((start, point))
    rows.sort(key=lambda item: item[0])
    return rows


def _sum_or_zero(entry: dict[str, Any] | None) -> float:
    if not entry:
        return 0.0
    try:
        return float(entry.get("sum") or 0.0)
    except (TypeError, ValueError):
        return 0.0


async def _last_sum(hass: Any, sid: str) -> float:
    recorder = get_instance(hass)
    last = await recorder.async_add_executor_job(
        get_last_statistics, hass, 1, sid, True, {"sum"}
    )
    rows = last.get(sid) if isinstance(last, dict) else None
    if isinstance(rows, list) and rows:
        return _sum_or_zero(rows[0] if isinstance(rows[0], dict) else None)
    return 0.0


async def _sum_before(hass: Any, sid: str, start: datetime) -> float:
    recorder = get_instance(hass)
    try:
        stats = await recorder.async_add_executor_job(
            partial(
                statistics_during_period,
                hass,
                start - timedelta(days=40),
                start,
                {sid},
                "day",
                None,
                {"sum"},
            )
        )
    except Exception:  # noqa: BLE001 — older recorder signatures
        return 0.0
    rows = stats.get(sid) if isinstance(stats, dict) else None
    if not isinstance(rows, list) or not rows:
        return 0.0
    previous = None
    for row in rows:
        if not isinstance(row, dict):
            continue
        row_start = row.get("start")
        try:
            stamp = float(row_start) if row_start is not None else 0.0
        except (TypeError, ValueError):
            continue
        if stamp < start.timestamp():
            previous = row
    return _sum_or_zero(previous)


def _points_to_statistics(
    rows: list[tuple[datetime, ConsumptionPoint]],
    *,
    stream: str,
    initial_sum: float,
) -> list[StatisticData]:
    running = initial_sum
    statistics: list[StatisticData] = []
    for start, point in rows:
        if stream == STREAM_COST:
            delta = float(point.cost_ttc or 0.0)
            if point.cost_ttc is None:
                continue
        else:
            delta = float(point.kwh or 0.0)
        running += delta
        statistics.append({"start": start, "sum": running, "state": delta})
    return statistics


def _contract_name(price: Any) -> str:
    return str(getattr(price, "libelle", "") or getattr(price, "energy", "") or "ENGIE")


async def async_import_history(
    hass: Any,
    coordinator: EngieCoordinator,
    *,
    days: int | None = None,
    energies: set[str] | None = None,
    include_costs: bool = True,
    contract_ids: set[str] | None = None,
) -> int:
    """Backfill daily consumption (and optional cost) for every contract."""
    snapshot = coordinator.data
    if snapshot is None:
        return 0
    window = days if days is not None else HISTORY_BACKFILL_DAYS
    total = 0
    for item in snapshot.contracts:
        price = item.price
        contract_id = str(getattr(price, "contract_id", "") or "")
        energy = str(getattr(price, "energy", "") or "")
        if not contract_id:
            continue
        if contract_ids and contract_id not in contract_ids:
            continue
        if energies and energy not in energies:
            continue
        points = await coordinator.api.async_get_consumption_points(
            contract_id, granularity="day", days=window
        )
        total += await _write_points(
            hass,
            coordinator,
            contract_id,
            _contract_name(price),
            points,
            include_costs=include_costs,
            reseed=True,
        )
    _LOGGER.info("Imported %s ENGIE daily statistic rows", total)
    return total


async def async_heal_recent(
    hass: Any, coordinator: EngieCoordinator, snapshot: AccountSnapshot
) -> int:
    """Overwrite the last few daily rows so late Linky data lands."""
    total = 0
    for item in snapshot.contracts:
        price = item.price
        contract_id = str(getattr(price, "contract_id", "") or "")
        if not contract_id:
            continue
        recent = list(item.recent_days or [])
        if len(recent) > HISTORY_HEAL_DAYS:
            recent = recent[-HISTORY_HEAL_DAYS:]
        total += await _write_points(
            hass,
            coordinator,
            contract_id,
            _contract_name(price),
            recent,
            include_costs=True,
            reseed=False,
        )
    return total


async def _write_points(
    hass: Any,
    coordinator: EngieCoordinator,
    contract_id: str,
    name: str,
    points: list[ConsumptionPoint],
    *,
    include_costs: bool,
    reseed: bool,
) -> int:
    rows = _usable_points(points)
    if not rows:
        return 0
    written = 0
    streams = [STREAM_ENERGY]
    if include_costs:
        streams.append(STREAM_COST)
    window_start = rows[0][0]
    for stream in streams:
        sid = statistic_id(contract_id, stream)
        if reseed:
            initial = 0.0
        else:
            initial = await _sum_before(hass, sid, window_start)
            if initial == 0.0:
                initial = await _last_sum(hass, sid)
        statistics = _points_to_statistics(rows, stream=stream, initial_sum=initial)
        if not statistics:
            continue
        async_add_external_statistics(hass, _metadata(contract_id, stream, name), statistics)
        last_sum = float(statistics[-1]["sum"])
        if stream == STREAM_ENERGY:
            coordinator.energy_totals[contract_id] = last_sum
        else:
            coordinator.cost_totals[contract_id] = last_sum
        written += len(statistics)
    return written


async def async_clear_history(
    hass: Any,
    coordinator: EngieCoordinator,
    *,
    energies: set[str] | None = None,
    include_costs: bool = True,
) -> list[str]:
    snapshot = coordinator.data
    if snapshot is None:
        return []
    stat_ids: list[str] = []
    for item in snapshot.contracts:
        price = item.price
        contract_id = str(getattr(price, "contract_id", "") or "")
        energy = str(getattr(price, "energy", "") or "")
        if not contract_id:
            continue
        if energies and energy not in energies:
            continue
        stat_ids.append(statistic_id(contract_id, STREAM_ENERGY))
        if include_costs:
            stat_ids.append(statistic_id(contract_id, STREAM_COST))
        coordinator.energy_totals.pop(contract_id, None)
        coordinator.cost_totals.pop(contract_id, None)
    if not stat_ids:
        return []
    recorder = get_instance(hass)
    future: asyncio.Future[None] = hass.loop.create_future()

    def _on_done() -> None:
        if not future.done():
            hass.loop.call_soon_threadsafe(future.set_result, None)

    recorder.queue_task(ClearStatisticsTask(on_done=_on_done, statistic_ids=stat_ids))
    try:
        async with asyncio.timeout(CLEAR_STATISTICS_TIMEOUT_SECONDS):
            await future
    except TimeoutError as err:
        raise HomeAssistantError("Clearing ENGIE statistics timed out") from err
    coordinator._history_bootstrapped = False  # noqa: SLF001
    return stat_ids
