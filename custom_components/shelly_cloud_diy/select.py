"""Select platform for Shelly Cloud DIY — writable virtual ``enum:<id>``.

Created in addition to the read-only sensor of the same component, on devices
the relay will route to and only while cloud control is on. See
``entities/control.py`` for the promises every control entity here keeps.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, SIGNAL_DEVICE_REMOVED
from .coordinator import ShellyCloudCoordinator, SIGNAL_NEW_DEVICE
from .entities.base import ShellyBaseEntity
from .entities.control import ShellyVirtualControlEntity, controllable_keys

_LOGGER = logging.getLogger(__name__)


def _controllable_keys(
    coordinator: ShellyCloudCoordinator, device_id: str
) -> list[str]:
    """Virtual enum keys this device may be given a control entity for."""
    return controllable_keys(coordinator, device_id, "cloud_control_enum_keys")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Shelly Cloud DIY selects."""
    coordinator: ShellyCloudCoordinator = hass.data[DOMAIN][entry.entry_id]
    created: set[str] = set()

    def create_selects(device_id: str) -> list[SelectEntity]:
        entities: list[SelectEntity] = []
        if not coordinator.is_enabled(device_id):
            return entities
        for key in _controllable_keys(coordinator, device_id):
            unique_id = f"{device_id}_{key}_control"
            if unique_id in created:
                continue
            created.add(unique_id)
            entities.append(ShellyVirtualSelect(coordinator, device_id, key))
        if entities:
            _LOGGER.info("Created %d selects for %s", len(entities), device_id)
        return entities

    @callback
    def async_add_device(device_id: str) -> None:
        if entities := create_selects(device_id):
            async_add_entities(entities)

    @callback
    def async_forget_device(device_id: str) -> None:
        for uid in [uid for uid in created if uid.startswith(device_id)]:
            created.discard(uid)

    entities: list[SelectEntity] = []
    for device_id in list(coordinator.devices.keys()):
        entities.extend(create_selects(device_id))
    if entities:
        async_add_entities(entities)

    entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_NEW_DEVICE, async_add_device)
    )
    entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_DEVICE_REMOVED, async_forget_device)
    )


class ShellyVirtualSelect(ShellyVirtualControlEntity, ShellyBaseEntity, SelectEntity):
    """Writable Gen2/Gen3 virtual enum (``enum:<id>``), over the relay."""

    _kind = "Enum"

    @property
    def options(self) -> list[str]:
        """The configured options, plus the live value if it is not among them.

        The config is a cached copy fetched once, so it can lag a component
        someone reconfigured — and Home Assistant logs an error for a
        ``current_option`` that is not in ``options``. Showing the value the
        device actually reports beats hiding it behind ``unknown``; the write
        path is unaffected, because the device refuses an unknown option
        itself (measured: ``Invalid argument 'value': not in options!``).
        """
        options: list[str] = []
        config = self.virtual_component_config(self._component_key)
        if isinstance(config, dict):
            configured = config.get("options")
            if isinstance(configured, list):
                options = [str(option) for option in configured]
        current = self.component_value()
        if isinstance(current, str) and current not in options:
            options.append(current)
        return options

    @property
    def current_option(self) -> str | None:
        """The component's current value, as the poll last saw it."""
        value = self.component_value()
        return value if isinstance(value, str) else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Expose the per-option display titles the Shelly app shows.

        They are not used as the option labels: Home Assistant matches an
        option by its string, and two options may carry the same title, so
        translating in the entity would make an ambiguous command. The titles
        are published for dashboards to use instead.
        """
        config = self.virtual_component_config(self._component_key)
        if not isinstance(config, dict):
            return None
        meta = config.get("meta")
        if not isinstance(meta, dict):
            return None
        ui = meta.get("ui")
        if not isinstance(ui, dict):
            return None
        titles = ui.get("titles")
        return {"titles": titles} if isinstance(titles, dict) and titles else None

    async def async_select_option(self, option: str) -> None:
        """Write the option over the relay and let the poll confirm it."""
        await self.coordinator.async_set_virtual_enum(
            self._device_id, self._component_key, option
        )
