"""Native per-meter selector for the ENGIE on-demand history period."""

from __future__ import annotations

from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import EngieCoordinator
from .entity import build_meter_source_device_info


CHOICES = {
    "90 jours": "90_days",
    "12 mois": "365_days",
    "Tout l'historique disponible": "all_available",
}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    runtime = hass.data[DOMAIN][entry.entry_id]
    coordinator: EngieCoordinator = runtime["coordinator"]
    discovered: set[str] = set()
    runtime.setdefault("meter_history_periods", {})

    def current_entities() -> list[SelectEntity]:
        result: list[SelectEntity] = []
        for source in getattr(coordinator.data, "meter_sources", None) or []:
            if source.source_id in discovered:
                continue
            discovered.add(source.source_id)
            result.append(EngieHistoryPeriodSelect(
                coordinator, entry, source, runtime
            ))
        return result

    async_add_entities(current_entities())

    @callback
    def on_update() -> None:
        latest = current_entities()
        if latest:
            async_add_entities(latest)

    entry.async_on_unload(coordinator.async_add_listener(on_update))


class EngieHistoryPeriodSelect(
    CoordinatorEntity[EngieCoordinator], SelectEntity
):
    """Choose 90 days, 12 months or all available history for this meter."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_name = "Période à récupérer"
    _attr_icon = "mdi:calendar-clock"
    _attr_options = list(CHOICES)

    def __init__(
        self, coordinator: EngieCoordinator, entry: ConfigEntry,
        source: Any, runtime: dict[str, Any],
    ) -> None:
        super().__init__(coordinator)
        self._source_id = source.source_id
        self._runtime = runtime
        self._attr_unique_id = (
            f"{entry.entry_id}_{self._source_id}_history_period"
        )
        self._attr_device_info = build_meter_source_device_info(entry, source)
        self._attr_current_option = runtime["meter_history_periods"].get(
            self._source_id, "12 mois"
        )

    async def async_select_option(self, option: str) -> None:
        if option not in CHOICES:
            raise ValueError("Unknown ENGIE history period")
        self._runtime["meter_history_periods"][self._source_id] = option
        self._attr_current_option = option
        self.async_write_ha_state()
