"""Price, consumption, and billing sensors."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from .vendor.engie_particuliers.models import PARIS, AccountSnapshot, parse_engie_datetime
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CURRENCY_EURO, EntityCategory, UnitOfEnergy
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import ATTRIBUTION, DOMAIN, UNIT_EUR_KWH
from .coordinator import EngieCoordinator
from .entity import (
    build_account_device_info,
    build_contract_device_info,
    build_meter_source_device_info,
)
from .meter_history import get_history_job_status


@dataclass(frozen=True, slots=True)
class SensorSpec:
    translation_key: str
    unique_suffix: str
    attr: str
    unit: str | None
    device_class: SensorDeviceClass | None
    display_precision: int
    cadran: str | None = None
    icon: str | None = None
    state_class: SensorStateClass | None = SensorStateClass.MEASUREMENT
    source: str = "price"


def _contract_specs() -> list[SensorSpec]:
    return [
        SensorSpec(
            translation_key="price_kwh_ttc",
            unique_suffix="price_kwh_ttc",
            attr="price_kwh_ttc",
            unit=UNIT_EUR_KWH,
            device_class=None,
            display_precision=5,
            icon="mdi:currency-eur",
        ),
        SensorSpec(
            translation_key="price_kwh_ht",
            unique_suffix="price_kwh_ht",
            attr="price_kwh_ht",
            unit=UNIT_EUR_KWH,
            device_class=None,
            display_precision=5,
            icon="mdi:currency-eur",
        ),
        SensorSpec(
            translation_key="abonnement_ttc",
            unique_suffix="abonnement_ttc",
            attr="abonnement_ttc",
            unit=CURRENCY_EURO,
            device_class=SensorDeviceClass.MONETARY,
            display_precision=2,
            icon="mdi:cash",
            state_class=None,
        ),
        SensorSpec(
            translation_key="daily_consumption",
            unique_suffix="last_day_kwh",
            attr="kwh",
            unit=UnitOfEnergy.KILO_WATT_HOUR,
            device_class=SensorDeviceClass.ENERGY,
            display_precision=1,
            icon="mdi:flash",
            state_class=SensorStateClass.TOTAL,
            source="last_day",
        ),
        SensorSpec(
            translation_key="daily_cost",
            unique_suffix="last_day_cost",
            attr="cost_ttc",
            unit=CURRENCY_EURO,
            device_class=SensorDeviceClass.MONETARY,
            display_precision=2,
            icon="mdi:cash",
            state_class=None,
            source="last_day",
        ),
        SensorSpec(
            translation_key="today_consumption",
            unique_suffix="today_kwh",
            attr="kwh",
            unit=UnitOfEnergy.KILO_WATT_HOUR,
            device_class=SensorDeviceClass.ENERGY,
            display_precision=1,
            icon="mdi:calendar-today",
            state_class=SensorStateClass.TOTAL,
            source="today",
        ),
        SensorSpec(
            translation_key="energy",
            unique_suffix="energy",
            attr="energy_total",
            unit=UnitOfEnergy.KILO_WATT_HOUR,
            device_class=SensorDeviceClass.ENERGY,
            display_precision=1,
            icon="mdi:lightning-bolt",
            state_class=SensorStateClass.TOTAL_INCREASING,
            source="energy_total",
        ),
    ]


def _cadran_specs(cadran: str) -> list[SensorSpec]:
    slug = cadran.lower()
    return [
        SensorSpec(
            translation_key="cadran_price_kwh_ttc",
            unique_suffix=f"{slug}_price_kwh_ttc",
            attr="price_kwh_ttc",
            unit=UNIT_EUR_KWH,
            device_class=None,
            display_precision=5,
            cadran=cadran,
            icon="mdi:chart-timeline-variant",
        ),
        SensorSpec(
            translation_key="cadran_price_kwh_ht",
            unique_suffix=f"{slug}_price_kwh_ht",
            attr="price_kwh_ht",
            unit=UNIT_EUR_KWH,
            device_class=None,
            display_precision=5,
            cadran=cadran,
            icon="mdi:chart-timeline-variant",
        ),
    ]


def _iter_specs(price: Any) -> list[SensorSpec]:
    specs = list(_contract_specs())
    seen: set[str] = set()
    for cadran in getattr(price, "cadrans", None) or []:
        name = str(getattr(cadran, "cadran", "") or "").strip()
        if not name or name.upper() in seen:
            continue
        seen.add(name.upper())
        specs.extend(_cadran_specs(name))
    return specs


def _snapshot(coordinator: EngieCoordinator) -> AccountSnapshot | None:
    data = coordinator.data
    return data if isinstance(data, AccountSnapshot) else None


def _as_date(value: str | None) -> date | None:
    parsed = parse_engie_datetime(value)
    return parsed.date() if parsed else None


def _lag_days(point: Any) -> int | None:
    parsed = parse_engie_datetime(getattr(point, "start", None))
    if parsed is None:
        return None
    return (datetime.now(PARIS).date() - parsed.astimezone(PARIS).date()).days


def _history_rows(item: Any) -> list[dict[str, Any]]:
    if item is None:
        return []
    rows: list[dict[str, Any]] = []
    for point in getattr(item, "recent_days", None) or []:
        if getattr(point, "kwh", None) is None or getattr(point, "partial", False):
            continue
        parsed = parse_engie_datetime(point.start)
        day = (
            parsed.astimezone(PARIS).date().isoformat()
            if parsed is not None
            else str(point.start or "")[:10]
        )
        rows.append(
            {
                "date": day,
                "kwh": point.kwh,
                "cost_ttc": point.cost_ttc,
            }
        )
    return rows


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: EngieCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    known: set[str] = set()

    def _entities() -> list[SensorEntity]:
        created: list[SensorEntity] = []
        snapshot = _snapshot(coordinator)
        # Optional migration away from contract-centric sensors.
        # Keeping the old unique IDs registered is a user choice; it is
        # safe to switch to source-only devices once dashboards are migrated.
        if bool(entry.options.get("legacy_contract_sensors", True)):
            for item in (snapshot.contracts if snapshot else []) or []:
                price = item.price
                contract_id = str(getattr(price, "contract_id", "") or "")
                if not contract_id:
                    continue
                for spec in _iter_specs(price):
                    unique_id = f"{entry.entry_id}_{contract_id}_{spec.unique_suffix}"
                    if unique_id in known:
                        continue
                    known.add(unique_id)
                    created.append(
                        EngieContractSensor(coordinator, entry, contract_id, spec)
                    )
        # Separate devices for each physical account/energy source.
        # Legacy contract entities retain their old unique IDs and devices.
        mode = str(entry.options.get("meter_source_mode") or "all")
        selected = set(entry.options.get("selected_meter_sources") or [])
        for source in (snapshot.meter_sources if snapshot else []) or []:
            if mode == "selected" and source.source_id not in selected:
                continue
            status_id = f"{entry.entry_id}_{source.source_id}_history_job_status"
            if status_id not in known:
                known.add(status_id)
                created.append(
                    EngieMeterHistoryStatusSensor(coordinator, entry, source)
                )
            if source.price is None:
                continue
            # Avoid a synthetic "cumulative" meter. The historical 31-day
            # rolling sum is not a lifetime total and must not be reported
            # as TOTAL_INCREASING or imported twice into HA Energy.
            for spec in _iter_specs(source.price):
                if spec.source == "energy_total":
                    continue
                unique_id = (
                    f"{entry.entry_id}_{source.source_id}_{spec.unique_suffix}"
                )
                if unique_id in known:
                    continue
                known.add(unique_id)
                created.append(
                    EngieMeterSourceSensor(
                        coordinator, entry, source.source_id, spec
                    )
                )
        for billing in (snapshot.billing if snapshot else []) or []:
            unique_id = f"{entry.entry_id}_{billing.account_id}_balance"
            if unique_id not in known:
                known.add(unique_id)
                created.append(
                    EngieBalanceSensor(coordinator, entry, billing.account_id)
                )
            unique_id = f"{entry.entry_id}_{billing.account_id}_next_invoice"
            if unique_id not in known:
                known.add(unique_id)
                created.append(
                    EngieNextInvoiceSensor(coordinator, entry, billing.account_id)
                )
            unique_id = f"{entry.entry_id}_{billing.account_id}_last_invoice"
            if unique_id not in known:
                known.add(unique_id)
                created.append(
                    EngieLastInvoiceSensor(coordinator, entry, billing.account_id)
                )
        return created

    async_add_entities(_entities())

    @callback
    def _on_update() -> None:
        new_entities = _entities()
        if new_entities:
            async_add_entities(new_entities)

    entry.async_on_unload(coordinator.async_add_listener(_on_update))


class EngieContractSensor(CoordinatorEntity[EngieCoordinator], SensorEntity):
    _attr_has_entity_name = True
    _attr_attribution = ATTRIBUTION

    def __init__(
        self,
        coordinator: EngieCoordinator,
        entry: ConfigEntry,
        contract_id: str,
        spec: SensorSpec,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._contract_id = contract_id
        self._spec = spec
        self._attr_unique_id = f"{entry.entry_id}_{contract_id}_{spec.unique_suffix}"
        self._attr_translation_key = spec.translation_key
        self._attr_native_unit_of_measurement = spec.unit
        self._attr_device_class = spec.device_class
        self._attr_state_class = spec.state_class
        self._attr_suggested_display_precision = spec.display_precision
        if spec.icon:
            self._attr_icon = spec.icon
        if spec.cadran:
            self._attr_translation_placeholders = {"cadran": spec.cadran}
        price = self._current_price()
        if price is not None:
            self._attr_device_info = build_contract_device_info(entry, price)

    @property
    def device_info(self):
        price = self._current_price()
        if price is not None:
            return build_contract_device_info(self._entry, price)
        return getattr(self, "_attr_device_info", None)

    @property
    def available(self) -> bool:
        if not super().available or self._current_price() is None:
            return False
        if self._spec.source == "today":
            return self._current_today() is not None
        return True

    @property
    def last_reset(self):
        if self._spec.state_class != SensorStateClass.TOTAL:
            return None
        point = (
            self._current_today()
            if self._spec.source == "today"
            else self._current_last_day()
            if self._spec.source == "last_day"
            else None
        )
        if point is None:
            return None
        return parse_engie_datetime(point.start or point.end)

    @property
    def native_value(self) -> float | None:
        if self._spec.source == "last_day":
            last_day = self._current_last_day()
            if last_day is None:
                return None
            return getattr(last_day, self._spec.attr, None)
        if self._spec.source == "today":
            today = self._current_today()
            if today is None:
                return None
            return getattr(today, self._spec.attr, None)
        if self._spec.source == "energy_total":
            return self._current_energy_total()
        price = self._current_price()
        if price is None:
            return None
        if self._spec.cadran:
            cadran = self._current_cadran(price)
            if cadran is None:
                return None
            return getattr(cadran, self._spec.attr, None)
        return getattr(price, self._spec.attr, None)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        price = self._current_price()
        if price is None:
            return {}
        attrs: dict[str, Any] = {
            "energy": getattr(price, "energy", None),
            "contract_id": getattr(price, "contract_id", None),
            "type_comptage": getattr(price, "type_comptage", None),
            "puissance": getattr(price, "puissance", None),
            "as_of": getattr(price, "as_of", None),
            "source": getattr(price, "source", None),
            "notes": getattr(price, "notes", None),
        }
        if self._spec.source == "last_day":
            last_day = self._current_last_day()
            if last_day is not None:
                attrs["as_of"] = last_day.end or last_day.start
                attrs["period_start"] = last_day.start
                attrs["period_end"] = last_day.end
                attrs["lag_days"] = _lag_days(last_day)
                attrs["complete"] = True
                attrs["source"] = f"histo{price.energy.title()}Jours"
        if self._spec.source == "today":
            today = self._current_today()
            if today is not None:
                attrs["as_of"] = today.end or today.start
                attrs["period_start"] = today.start
                attrs["period_end"] = today.end
                attrs["complete"] = False
                attrs["source"] = "histoElecHeures"
        if self._spec.source == "energy_total":
            attrs["statistic_id"] = self.coordinator.energy_statistic_id(self._contract_id)
            attrs["source"] = "histoElecJours" if price.energy == "ELEC" else "histoGazJours"
        if self._spec.source in {"last_day", "energy_total"}:
            history = _history_rows(self._current_item())
            if history:
                attrs["history"] = history
        if self._spec.cadran:
            cadran = self._current_cadran(price)
            attrs["cadran"] = self._spec.cadran
            if cadran is not None:
                attrs["as_of"] = getattr(cadran, "as_of", None) or attrs["as_of"]
                attrs["source"] = getattr(cadran, "source", None) or attrs["source"]
        return {key: value for key, value in attrs.items() if value not in (None, "")}

    def _current_item(self) -> Any | None:
        snapshot = _snapshot(self.coordinator)
        if snapshot is None:
            return None
        for item in snapshot.contracts:
            if str(getattr(item.price, "contract_id", "") or "") == self._contract_id:
                return item
        return None

    def _current_price(self) -> Any | None:
        item = self._current_item()
        return None if item is None else item.price

    def _current_last_day(self) -> Any | None:
        item = self._current_item()
        return None if item is None else item.last_day

    def _current_today(self) -> Any | None:
        item = self._current_item()
        return None if item is None else getattr(item, "today", None)

    def _current_energy_total(self) -> float | None:
        stored = self.coordinator.energy_totals.get(self._contract_id)
        if stored is not None:
            return stored
        item = self._current_item()
        if item is None:
            return None
        values = [
            float(point.kwh)
            for point in (item.recent_days or [])
            if point.kwh is not None
        ]
        return round(sum(values), 3) if values else None

    def _current_cadran(self, price: Any) -> Any | None:
        wanted = (self._spec.cadran or "").upper()
        for cadran in getattr(price, "cadrans", None) or []:
            if str(getattr(cadran, "cadran", "") or "").upper() == wanted:
                return cadran
        return None


class EngieMeterSourceSensor(EngieContractSensor):
    """One sensor belonging to an exact account/energy physical meter."""

    def __init__(
        self,
        coordinator: EngieCoordinator,
        entry: ConfigEntry,
        source_id: str,
        spec: SensorSpec,
    ) -> None:
        super().__init__(coordinator, entry, source_id, spec)
        source = self._source()
        if source is not None:
            self._attr_device_info = build_meter_source_device_info(entry, source)

    def _source(self) -> Any | None:
        snapshot = _snapshot(self.coordinator)
        if snapshot is None:
            return None
        return next(
            (
                source
                for source in snapshot.meter_sources
                if source.source_id == self._contract_id
            ),
            None,
        )

    def _current_item(self) -> Any | None:
        return self._source()

    @property
    def device_info(self):
        source = self._source()
        if source is not None:
            return build_meter_source_device_info(self._entry, source)
        return getattr(self, "_attr_device_info", None)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = super().extra_state_attributes
        source = self._source()
        if source is not None:
            attrs.update(
                {
                    "account_suffix": source.account_suffix,
                    "meter_suffix": source.meter_suffix,
                    "meter_energy": source.energy,
                    "declared_on_account": source.declared_contract,
                    "matches_selected_gas_pce": source.matched_selected_pce,
                    "meter_source": True,
                }
            )
        return attrs


class EngieMeterHistoryStatusSensor(CoordinatorEntity[EngieCoordinator], SensorEntity):
    """User-visible progress for this physical meter's on-demand history."""

    _attr_has_entity_name = True
    _attr_attribution = ATTRIBUTION
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:database-clock"

    def __init__(
        self, coordinator: EngieCoordinator, entry: ConfigEntry, source: Any
    ) -> None:
        super().__init__(coordinator)
        self._source_id = source.source_id
        self._attr_unique_id = (
            f"{entry.entry_id}_{source.source_id}_history_job_status"
        )
        self._attr_name = "État de récupération de l'historique"
        self._attr_device_info = build_meter_source_device_info(entry, source)

    def _job(self) -> dict[str, Any]:
        return get_history_job_status(
            self.coordinator.hass, self.coordinator, source_id=self._source_id
        )

    @property
    def native_value(self) -> str:
        status = self._job().get("status", "no_job")
        return {
            "no_job": "Aucune tâche",
            "running": "En cours",
            "completed": "Terminée",
            "failed": "Échec",
            "not_found": "Aucune tâche",
        }.get(status, "Inconnu")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        job = self._job()
        attrs = {
            key: job[key]
            for key in (
                "id",
                "period_label",
                "preview_only",
                "windows_completed",
                "window_count",
                "error_type",
                "http_status",
            )
            if key in job
        }
        count = job.get("window_count")
        completed = job.get("windows_completed")
        if type(count) is int and count > 0 and type(completed) is int:
            attrs["progress_percent"] = round(100 * completed / count)
        result = job.get("result")
        if isinstance(result, dict):
            attrs["new_or_corrected_days"] = result.get(
                "new_or_corrected_days", 0
            )
            attrs["statistics_queued"] = result.get("statistics_queued", 0)
        return attrs


class _EngieBillingSensor(CoordinatorEntity[EngieCoordinator], SensorEntity):
    _attr_has_entity_name = True
    _attr_attribution = ATTRIBUTION
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: EngieCoordinator,
        entry: ConfigEntry,
        account_id: str,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._account_id = account_id
        self._attr_device_info = build_account_device_info(entry)

    def _billing(self) -> Any | None:
        snapshot = _snapshot(self.coordinator)
        if snapshot is None:
            return None
        for item in snapshot.billing:
            if item.account_id == self._account_id:
                return item
        return None

    @property
    def available(self) -> bool:
        return super().available and self._billing() is not None


class EngieBalanceSensor(_EngieBillingSensor):
    _attr_translation_key = "balance"
    _attr_native_unit_of_measurement = CURRENCY_EURO
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_suggested_display_precision = 2
    _attr_icon = "mdi:scale-balance"

    def __init__(
        self,
        coordinator: EngieCoordinator,
        entry: ConfigEntry,
        account_id: str,
    ) -> None:
        super().__init__(coordinator, entry, account_id)
        self._attr_unique_id = f"{entry.entry_id}_{account_id}_balance"

    @property
    def native_value(self) -> float | None:
        billing = self._billing()
        if billing is None:
            return None
        return billing.balance.solde

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        billing = self._billing()
        if billing is None:
            return {}
        return {
            key: value
            for key, value in {
                "solde_hors_ce": billing.balance.solde_hors_ce,
                "invoice_count": billing.invoice_count,
            }.items()
            if value not in (None, "")
        }


class EngieNextInvoiceSensor(_EngieBillingSensor):
    _attr_translation_key = "next_invoice"
    _attr_device_class = SensorDeviceClass.DATE
    _attr_icon = "mdi:calendar-clock"

    def __init__(
        self,
        coordinator: EngieCoordinator,
        entry: ConfigEntry,
        account_id: str,
    ) -> None:
        super().__init__(coordinator, entry, account_id)
        self._attr_unique_id = f"{entry.entry_id}_{account_id}_next_invoice"

    @property
    def native_value(self) -> date | None:
        billing = self._billing()
        if billing is None:
            return None
        return _as_date(billing.balance.next_invoice_date)


class EngieLastInvoiceSensor(_EngieBillingSensor):
    _attr_translation_key = "last_invoice"
    _attr_native_unit_of_measurement = CURRENCY_EURO
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_suggested_display_precision = 2
    _attr_icon = "mdi:receipt-text"

    def __init__(
        self,
        coordinator: EngieCoordinator,
        entry: ConfigEntry,
        account_id: str,
    ) -> None:
        super().__init__(coordinator, entry, account_id)
        self._attr_unique_id = f"{entry.entry_id}_{account_id}_last_invoice"

    @property
    def native_value(self) -> float | None:
        billing = self._billing()
        if billing is None or billing.last_invoice is None:
            return None
        return billing.last_invoice.amount_ttc

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        billing = self._billing()
        invoice = None if billing is None else billing.last_invoice
        if invoice is None:
            return {}
        return {
            key: value
            for key, value in {
                "date": invoice.date,
                "due_date": invoice.due_date,
                "label": invoice.label,
                "payment_status": invoice.payment_status,
            }.items()
            if value not in (None, "")
        }
