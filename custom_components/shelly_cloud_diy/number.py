"""Number platform for Shelly Cloud DIY — writable virtual ``number:<id>``.

Created **in addition** to the read-only sensor of the same component
(``sensor.py``), never instead of it, for the reason spelled out on
``ShellyVirtualBooleanSwitch``: replacing an entity an automation already
points at breaks that automation silently.

Only devices the relay said it will route to get one, and only while the user
has cloud control switched on — the documented HTTP API cannot write a virtual
component at all, so without the relay there is nothing here to build.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    SIGNAL_DEVICE_REMOVED,
    VIRTUAL_NUMBER_FALLBACK_MAX,
    VIRTUAL_NUMBER_FALLBACK_MIN,
)
from .coordinator import ShellyCloudCoordinator, SIGNAL_NEW_DEVICE
from .entities.base import ShellyBaseEntity
from .entities.control import ShellyVirtualControlEntity, controllable_keys

_LOGGER = logging.getLogger(__name__)


def _controllable_keys(
    coordinator: ShellyCloudCoordinator, device_id: str
) -> list[str]:
    """Virtual number keys this device may be given a control entity for."""
    return controllable_keys(coordinator, device_id, "cloud_control_number_keys")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Shelly Cloud DIY numbers."""
    coordinator: ShellyCloudCoordinator = hass.data[DOMAIN][entry.entry_id]
    created: set[str] = set()

    def create_numbers(device_id: str) -> list[NumberEntity]:
        entities: list[NumberEntity] = []
        if not coordinator.is_enabled(device_id):
            return entities
        for key in _controllable_keys(coordinator, device_id):
            unique_id = f"{device_id}_{key}_control"
            if unique_id in created:
                continue
            created.add(unique_id)
            entities.append(ShellyVirtualNumber(coordinator, device_id, key))
        if entities:
            _LOGGER.info("Created %d numbers for %s", len(entities), device_id)
        return entities

    @callback
    def async_add_device(device_id: str) -> None:
        if entities := create_numbers(device_id):
            async_add_entities(entities)

    @callback
    def async_forget_device(device_id: str) -> None:
        for uid in [uid for uid in created if uid.startswith(device_id)]:
            created.discard(uid)

    entities: list[NumberEntity] = []
    for device_id in list(coordinator.devices.keys()):
        entities.extend(create_numbers(device_id))
    if entities:
        async_add_entities(entities)

    entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_NEW_DEVICE, async_add_device)
    )
    entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_DEVICE_REMOVED, async_forget_device)
    )


class ShellyVirtualNumber(ShellyVirtualControlEntity, ShellyBaseEntity, NumberEntity):
    """Writable Gen2/Gen3 virtual number (``number:<id>``), over the relay."""

    _kind = "Number"

    @property
    def native_value(self) -> float | None:
        """Return the value as the poll last saw it, never an optimistic one."""
        value = self.component_value()
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return float(value)

    @property
    def native_min_value(self) -> float:
        """Lower bound from the v2 config, or a wide fallback."""
        return self._config_number("min", VIRTUAL_NUMBER_FALLBACK_MIN)

    @property
    def native_max_value(self) -> float:
        """Upper bound from the v2 config, or a wide fallback."""
        return self._config_number("max", VIRTUAL_NUMBER_FALLBACK_MAX)

    @property
    def native_step(self) -> float | None:
        """Step from ``meta.ui.step``; ``None`` lets Home Assistant decide."""
        step = self._ui_field("step")
        if isinstance(step, bool) or not isinstance(step, (int, float)):
            return None
        return float(step) or None

    @property
    def native_unit_of_measurement(self) -> str | None:
        """Unit from ``meta.ui.unit``, the same field the sensor reads."""
        unit = self._ui_field("unit")
        return unit if isinstance(unit, str) and unit else None

    @property
    def mode(self) -> NumberMode:
        """Slider only when the component says so *and* the range is real.

        A slider across the fallback range would be unusable, so an unknown
        range renders as a box even if the component asked for a slider.
        """
        if self._ui_field("view") != "slider":
            return NumberMode.BOX
        config = self.virtual_component_config(self._component_key)
        if not isinstance(config, dict):
            return NumberMode.BOX
        if config.get("min") is None or config.get("max") is None:
            return NumberMode.BOX
        return NumberMode.SLIDER

    def _config_number(self, field: str, fallback: float) -> float:
        config = self.virtual_component_config(self._component_key)
        if isinstance(config, dict):
            value = config.get(field)
            if not isinstance(value, bool) and isinstance(value, (int, float)):
                return float(value)
        return fallback

    def _ui_field(self, field: str) -> Any:
        config = self.virtual_component_config(self._component_key)
        if not isinstance(config, dict):
            return None
        meta = config.get("meta")
        if not isinstance(meta, dict):
            return None
        ui = meta.get("ui")
        if not isinstance(ui, dict):
            return None
        return ui.get(field)

    async def async_set_native_value(self, value: float) -> None:
        """Write the value over the relay and let the poll confirm it."""
        await self.coordinator.async_set_virtual_number(
            self._device_id, self._component_key, value
        )
