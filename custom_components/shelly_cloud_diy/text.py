"""Text platform for Shelly Cloud DIY — writable virtual ``text:<id>``.

Created in addition to the read-only sensor of the same component, on devices
the relay will route to and only while cloud control is on. See
``entities/control.py`` for the promises every control entity here keeps.
"""
from __future__ import annotations

import logging

from homeassistant.components.text import TextEntity, TextMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, SIGNAL_DEVICE_REMOVED, VIRTUAL_TEXT_FALLBACK_MAX_LEN
from .coordinator import ShellyCloudCoordinator, SIGNAL_NEW_DEVICE
from .entities.base import ShellyBaseEntity
from .entities.control import ShellyVirtualControlEntity, controllable_keys

_LOGGER = logging.getLogger(__name__)


def _controllable_keys(
    coordinator: ShellyCloudCoordinator, device_id: str
) -> list[str]:
    """Virtual text keys this device may be given a control entity for."""
    return controllable_keys(coordinator, device_id, "cloud_control_text_keys")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Shelly Cloud DIY texts."""
    coordinator: ShellyCloudCoordinator = hass.data[DOMAIN][entry.entry_id]
    created: set[str] = set()

    def create_texts(device_id: str) -> list[TextEntity]:
        entities: list[TextEntity] = []
        if not coordinator.is_enabled(device_id):
            return entities
        for key in _controllable_keys(coordinator, device_id):
            unique_id = f"{device_id}_{key}_control"
            if unique_id in created:
                continue
            created.add(unique_id)
            entities.append(ShellyVirtualText(coordinator, device_id, key))
        if entities:
            _LOGGER.info("Created %d texts for %s", len(entities), device_id)
        return entities

    @callback
    def async_add_device(device_id: str) -> None:
        if entities := create_texts(device_id):
            async_add_entities(entities)

    @callback
    def async_forget_device(device_id: str) -> None:
        for uid in [uid for uid in created if uid.startswith(device_id)]:
            created.discard(uid)

    entities: list[TextEntity] = []
    for device_id in list(coordinator.devices.keys()):
        entities.extend(create_texts(device_id))
    if entities:
        async_add_entities(entities)

    entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_NEW_DEVICE, async_add_device)
    )
    entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_DEVICE_REMOVED, async_forget_device)
    )


class ShellyVirtualText(ShellyVirtualControlEntity, ShellyBaseEntity, TextEntity):
    """Writable Gen2/Gen3 virtual text (``text:<id>``), over the relay."""

    _kind = "Text"
    _attr_mode = TextMode.TEXT
    _attr_native_min = 0

    @property
    def native_value(self) -> str | None:
        """Return the value as the poll last saw it."""
        value = self.component_value()
        return value if isinstance(value, str) else None

    @property
    def native_max(self) -> int:
        """Cap from the component's ``max_len``, or the device default.

        The device enforces its own cap and refuses anything longer, so this
        is a UI hint. It is still read from the config rather than assumed,
        because a component created with a smaller cap would otherwise offer
        the user a field that cannot be submitted.
        """
        config = self.virtual_component_config(self._component_key)
        if isinstance(config, dict):
            max_len = config.get("max_len")
            if isinstance(max_len, int) and not isinstance(max_len, bool):
                if 0 < max_len <= 255:
                    return max_len
        return VIRTUAL_TEXT_FALLBACK_MAX_LEN

    async def async_set_value(self, value: str) -> None:
        """Write the value over the relay and let the poll confirm it."""
        await self.coordinator.async_set_virtual_text(
            self._device_id, self._component_key, value
        )
