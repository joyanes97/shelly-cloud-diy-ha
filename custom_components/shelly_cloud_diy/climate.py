"""Climate platform for Shelly Cloud DIY.

One entity kind lives here: a **Shelly BLU TRV** seen through the Shelly BLU
Gateway Gen3 that carries it. The valve itself speaks Bluetooth and has no
cloud identity, so everything about it — reading and writing — happens on the
gateway's record.

Reading needs nothing new: the gateway publishes each paired valve as
``blutrv:<id>`` with the thermostat state flat inside it, and that arrives in
the ordinary poll. Writing goes over the opt-in cloud relay, which is why the
setpoint appears only once cloud control is connected and the relay has said
it will route to this gateway — exactly like the virtual booleans in
:mod:`switch`. Without it the entity is still useful: it reads the room
temperature and the valve's own setpoint, it just cannot change it.

Shape confirmed against a real sanitised diagnostics extract from a gateway
with two valves, contributed by @gerok1984 (#48).
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import (
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    BLUTRV_KEY_RE,
    BLUTRV_MAX_TEMP_C,
    BLUTRV_MIN_TEMP_C,
    DOMAIN,
    SIGNAL_DEVICE_REMOVED,
)
from .coordinator import ShellyCloudCoordinator, SIGNAL_NEW_DEVICE
from .entities.base import ShellyBaseEntity


def _create_climate_entities(
    device_id: str,
    status: dict[str, Any],
    created: set[str],
    coordinator: ShellyCloudCoordinator,
) -> list[ClimateEntity]:
    """Build one climate entity per BLU TRV this gateway carries.

    Same signature as the sensor and binary-sensor builders so it can be
    driven directly from a test — and so the coverage report in
    :mod:`diagnostics` could call it, should a climate-only device ever
    appear.
    """
    entities: list[ClimateEntity] = []
    if not coordinator.is_enabled(device_id):
        return entities
    if not isinstance(status, dict):
        return entities

    keys = sorted(
        (
            key
            for key, value in status.items()
            if BLUTRV_KEY_RE.match(key) and isinstance(value, dict)
        ),
        key=lambda key: int(key.split(":", 1)[1]),
    )

    for display_index, key in enumerate(keys):
        unique_id = f"{device_id}_{key}_climate"
        if unique_id in created:
            continue
        created.add(unique_id)
        entities.append(
            ShellyBluTrvClimate(coordinator, device_id, key, display_index)
        )
    return entities


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Shelly Cloud DIY climate entities."""
    coordinator: ShellyCloudCoordinator = hass.data[DOMAIN][entry.entry_id]
    created: set[str] = set()

    @callback
    def async_add_device(device_id: str) -> None:
        status = coordinator.devices.get(device_id, {}).get("status", {})
        if entities := _create_climate_entities(
            device_id, status, created, coordinator
        ):
            async_add_entities(entities)

    @callback
    def async_forget_device(device_id: str) -> None:
        for unique_id in [uid for uid in created if uid.startswith(f"{device_id}_")]:
            created.discard(unique_id)

    entities: list[ClimateEntity] = []
    for device_id in list(coordinator.devices.keys()):
        status = coordinator.devices.get(device_id, {}).get("status", {})
        entities.extend(
            _create_climate_entities(device_id, status, created, coordinator)
        )
    if entities:
        async_add_entities(entities)

    entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_NEW_DEVICE, async_add_device)
    )
    entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_DEVICE_REMOVED, async_forget_device)
    )


class ShellyBluTrvClimate(ShellyBaseEntity, ClimateEntity):
    """A Shelly BLU TRV, reached through its BLU Gateway Gen3."""

    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_hvac_modes = [HVACMode.HEAT]
    _attr_hvac_mode = HVACMode.HEAT
    _attr_min_temp = BLUTRV_MIN_TEMP_C
    _attr_max_temp = BLUTRV_MAX_TEMP_C
    _attr_target_temperature_step = 0.5

    def __init__(
        self,
        coordinator: ShellyCloudCoordinator,
        device_id: str,
        component_key: str,
        display_index: int,
    ) -> None:
        """Initialize the valve.

        ``display_index`` is the position among this gateway's valves, not
        the component id: the ids start at 200 and would make an absurd
        entity name.
        """
        super().__init__(coordinator, device_id, display_index)
        self._component_key = component_key
        self._attr_unique_id = f"{device_id}_{component_key}_climate"
        self._fallback_name = (
            "BLU TRV" if display_index == 0 else f"BLU TRV {display_index + 1}"
        )

    @property
    def name(self) -> str:
        """The valve's Shelly-App alias, or its position on the gateway.

        A property rather than a fixed name because the alias is resolved by
        a background task after the entity exists — and because it may never
        resolve, which is why the positional fallback stays.

        Two roads to the name were measured and found closed: the valve's own
        v2 config answers ``name: null``, and so does the
        ``bthomedevice:<id>`` it points at. The one that works is the
        account's alias listing, where each valve has a child record of its
        own — resolved in the coordinator, which is the side that makes that
        request anyway. (#48)
        """
        aliases = getattr(self.coordinator, "blu_trv_names", None)
        if isinstance(aliases, dict):
            gateway = aliases.get(self._device_id)
            if isinstance(gateway, dict):
                alias = gateway.get(self._component_key)
                if isinstance(alias, str) and alias.strip():
                    return alias.strip()
        return self.virtual_component_name(self._component_key) or self._fallback_name

    def _component(self) -> dict[str, Any]:
        value = self.device_status.get(self._component_key)
        return value if isinstance(value, dict) else {}

    @property
    def available(self) -> bool:
        """Available while the gateway is reporting and the valve is on it.

        ``connected`` is the valve's own radio link. The gateway stays online
        and keeps serving the last values it saw, so a valve that dropped off
        would otherwise present a stale temperature as a live one.
        """
        component = self._component()
        return (
            bool(component)
            and component.get("connected") is not False
            and component.get("paired") is not False
            and super().available
        )

    @property
    def supported_features(self) -> ClimateEntityFeature:
        """Offer the setpoint only when the relay can actually carry it."""
        if self.coordinator.cloud_control_connected and (
            self.coordinator.is_cloud_controllable(self._device_id)
        ):
            return ClimateEntityFeature.TARGET_TEMPERATURE
        return ClimateEntityFeature(0)

    @property
    def current_temperature(self) -> float | None:
        value = self._component().get("current_C")
        return float(value) if isinstance(value, (int, float)) else None

    @property
    def target_temperature(self) -> float | None:
        value = self._component().get("target_C")
        return float(value) if isinstance(value, (int, float)) else None

    @property
    def hvac_action(self) -> HVACAction:
        """Derive heating from the valve position, the only signal there is.

        ``pos`` is how far the valve is open, in percent. A BLU TRV reports
        no flow and no demand flag, so anything above closed is the honest
        answer to "is this radiator being fed".
        """
        pos = self._component().get("pos")
        if isinstance(pos, (int, float)) and pos > 0:
            return HVACAction.HEATING
        return HVACAction.IDLE

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Valve-specific readings with no entity of their own.

        Battery and signal are deliberately absent: those are sensors, built
        by the sensor platform from the same component, so that they land in
        the recorder and on a dashboard like every other device's.
        """
        component = self._component()
        attrs: dict[str, Any] = {}
        if isinstance(component.get("pos"), (int, float)):
            attrs["valve_position"] = component["pos"]
        if isinstance(component.get("last_updated_ts"), (int, float)):
            attrs["last_updated_ts"] = component["last_updated_ts"]
        if isinstance(component.get("fw_ver"), str):
            attrs["firmware_version"] = component["fw_ver"]
        return attrs

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Send a new setpoint to the valve, via the gateway."""
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if not isinstance(temperature, (int, float)):
            return
        await self.coordinator.async_set_blutrv_target(
            self._device_id, self._component_key, float(temperature)
        )
