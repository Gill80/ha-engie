"""Manual ENGIE historical-import controls on each physical meter device."""

from __future__ import annotations

from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import EngieCoordinator
from .entity import build_meter_source_device_info
from .meter_history import start_history_job
from .select import CHOICES


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    runtime = hass.data[DOMAIN][entry.entry_id]
    coordinator: EngieCoordinator = runtime["coordinator"]
    known: set[tuple[str, bool]] = set()

    def new_entities() -> list[ButtonEntity]:
        result: list[ButtonEntity] = []
        for source in getattr(coordinator.data, "meter_sources", None) or []:
            for preview_only in (True, False):
                key = (source.source_id, preview_only)
                if key in known:
                    continue
                known.add(key)
                result.append(EngieHistoryButton(
                    coordinator, entry, source, runtime,
                    preview_only=preview_only,
                ))
        return result

    async_add_entities(new_entities())

    @callback
    def on_update() -> None:
        added = new_entities()
        if added:
            async_add_entities(added)

    entry.async_on_unload(coordinator.async_add_listener(on_update))


class EngieHistoryButton(CoordinatorEntity[EngieCoordinator], ButtonEntity):
    """One button previews, one imports; both use the period selector."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: EngieCoordinator,
        entry: ConfigEntry,
        source: Any,
        runtime: dict[str, Any],
        *,
        preview_only: bool,
    ) -> None:
        super().__init__(coordinator)
        self._source_id = source.source_id
        self._runtime = runtime
        self._preview_only = preview_only
        self._attr_unique_id = (
            f"{entry.entry_id}_{source.source_id}_history_"
            f"{'preview' if preview_only else 'import'}"
        )
        self._attr_name = (
            "Prévisualiser l'historique" if preview_only
            else "Récupérer l'historique"
        )
        self._attr_icon = (
            "mdi:database-eye" if preview_only
            else "mdi:database-import"
        )
        self._attr_device_info = build_meter_source_device_info(entry, source)

    async def async_press(self) -> None:
        chosen = self._runtime.get("meter_history_periods", {}).get(
            self._source_id, "12 mois"
        )
        start_history_job(
            self.hass,
            self.coordinator,
            source_id=self._source_id,
            period=CHOICES.get(chosen, "365_days"),
            include_costs=True,
            preview_only=self._preview_only,
        )
