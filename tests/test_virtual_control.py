"""Writable virtual components and script entities (issue #48).

Derived from the issue and from ONE measurement session on real hardware, not
from the implementation: a Shelly 1PM Mini G3 (``S3SW-001P8EU``, FW 1.7.5) on
the operator's own account, 2026-09-27. A virtual ``number`` / ``text`` /
``enum`` and a throw-away script were created over local RPC, read back through
both cloud endpoints, written through the cloud relay, and removed again.

Two things that session established, and that everything here rests on:

* **The cloud carries the whole component.** ``/device/status`` lists the live
  ``value`` and the v2 ``settings`` blob carries the config the entity needs —
  ``name``, ``min``/``max``, ``meta.ui.unit``/``step``/``view``, the enum
  ``options`` and ``titles``, a text's ``max_len``, and a script's ``name``.
  The payloads below are those answers, with the device id replaced.
* **The relay writes all four.** ``Number.Set`` / ``Text.Set`` / ``Enum.Set``
  and ``Script.Start`` / ``Script.Stop`` each returned ``{"result": …}`` and the
  device's local value really changed. The negative control was the same method
  on the same device with only the *value* invalid, which came back
  ``{"error": {"error": "JRPC_ERROR", "device_error": {"code": -103, …}}}`` and
  left the value alone — so a success answer means the write happened.

The read-only sensors for these components already exist (``sensor.py``, #9)
and stay: a control entity is created **beside** its sensor, never instead of
it, exactly as the virtual boolean switch is. ``test_control_entities_do_not_
replace_the_read_only_sensors`` is the guard.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.shelly_cloud_diy import number as number_platform
from custom_components.shelly_cloud_diy import select as select_platform
from custom_components.shelly_cloud_diy import switch as switch_platform
from custom_components.shelly_cloud_diy import text as text_platform
from custom_components.shelly_cloud_diy.api.cloud_ws import (
    DeviceOwnership,
    ShellyCloudWsCommandError,
)
from custom_components.shelly_cloud_diy.binary_sensor import (
    ShellyScriptBinarySensor,
    _create_rpc_sensors as create_rpc_binary_sensors,
)
from custom_components.shelly_cloud_diy.const import CONF_CLOUD_CONTROL
from custom_components.shelly_cloud_diy.coordinator import ShellyCloudCoordinator
from custom_components.shelly_cloud_diy.sensor import (
    RpcVirtualSensor,
    _create_rpc_sensors as create_rpc_sensors,
)

OWNED_ID = "5432044e0001"
SHARED_ID = "5432044e0002"

# ── The measured payloads ─────────────────────────────────────────────
#
# ``/device/status`` for the rig, trimmed to the components under test. Note
# what the cloud does NOT send for a freshly written virtual component:
# ``last_update_ts``. It was in the local RPC answer and not in the cloud one,
# so nothing here may depend on it.
RIG_STATUS: dict[str, Any] = {
    "sys": {"mac": "ECDA3BC59EC8"},
    "switch:0": {"id": 0, "output": False},
    "boolean:200": {"value": False, "source": "rpc"},
    "number:200": {"value": 22.5, "source": "rpc"},
    "text:200": {"value": "Wohnzimmer Ost", "source": "rpc"},
    "enum:200": {"value": "comfort", "source": "rpc"},
    "script:1": {"id": 1, "running": True, "mem_used": 770, "cpu": 3},
    "script:2": {"id": 2, "running": False, "mem_used": 42, "cpu": 0},
    "scripts": ["script:1", "script:2"],
    "vcomps": ["boolean:200", "enum:200", "number:200", "text:200"],
}

# The v2 ``settings`` answer for the same device, same session.
RIG_CONFIG: dict[str, dict] = {
    "boolean:200": {
        "id": 200,
        "name": "Cloud Write Test",
        "meta": {"ui": {"view": "toggle"}},
        "persisted": False,
        "default_value": False,
    },
    "number:200": {
        "id": 200,
        "name": "DIY Number Probe",
        "min": 4,
        "max": 30,
        "meta": {"ui": {"view": "slider", "unit": "°C", "step": 0.5}},
        "persisted": False,
        "default_value": 21.5,
    },
    "text:200": {
        "id": 200,
        "name": "DIY Text Probe",
        "max_len": 255,
        "meta": {"ui": {"view": "field"}},
        "persisted": False,
        "default_value": "hello cloud",
    },
    "enum:200": {
        "id": 200,
        "name": "DIY Enum Probe",
        "options": ["eco", "comfort", "boost"],
        "meta": {
            "ui": {
                "view": "dropdown",
                "titles": {"eco": "Eco Mode", "comfort": "Comfort", "boost": "Boost"},
            }
        },
        "persisted": False,
        "default_value": "eco",
    },
    "script:1": {"id": 1, "name": "aioshelly_ble_integration", "enable": False},
    "script:2": {"id": 2, "name": "diy_probe_dummy", "enable": False},
}


# ── Harness ───────────────────────────────────────────────────────────


class _FakeRelay:
    """Records what was sent; never talks to anything."""

    host = "shelly-42-eu.shelly.cloud"

    def __init__(
        self,
        verdicts: dict[str, Any] | None = None,
        *,
        command_error: Exception | None = None,
        connected: bool = True,
    ) -> None:
        self._verdicts = verdicts or {}
        self._command_error = command_error
        self._connected = connected
        self.sent: list[tuple[str, str, dict]] = []

    @property
    def connected(self) -> bool:
        return self._connected

    async def send_jrpc_request(
        self, device_id: str, method: str, params: dict | None = None, **_: Any
    ) -> dict:
        self.sent.append((device_id, method, params or {}))
        if self._command_error is not None:
            raise self._command_error
        return {}


def _coordinator(
    *,
    status: dict[str, Any] | None = None,
    configs: dict[str, dict] | None = None,
    options: dict[str, Any] | None = None,
    relay: _FakeRelay | None = None,
    ownership: DeviceOwnership = DeviceOwnership.OWNED,
    device_id: str = OWNED_ID,
) -> ShellyCloudCoordinator:
    """Build a coordinator by hand, as the rest of this suite does."""
    coordinator = object.__new__(ShellyCloudCoordinator)
    coordinator._entry = SimpleNamespace(
        entry_id="e1",
        options=dict(options or {CONF_CLOUD_CONTROL: True}),
        data={},
        async_create_background_task=lambda *a, **k: None,
    )
    coordinator.devices = {
        device_id: {"status": dict(status or RIG_STATUS), "online": True}
    }
    coordinator.device_names = {}
    coordinator.virtual_configs = (
        {device_id: dict(configs)} if configs is not None else {}
    )
    coordinator.device_ownership = {device_id: ownership}
    coordinator._ownership_unresolved = set()
    coordinator._ownership_task = None
    coordinator._cloud_ws = relay
    coordinator.last_update_success = True
    coordinator.data = coordinator.devices
    coordinator.hass = SimpleNamespace(data={"shelly_cloud_diy": {"e1": coordinator}})

    refreshes: list[int] = []
    coordinator.refreshes = refreshes

    async def _request_refresh() -> None:
        refreshes.append(1)

    coordinator.async_request_refresh = _request_refresh
    return coordinator


def _enabled_coordinator(**kwargs: Any) -> tuple[ShellyCloudCoordinator, _FakeRelay]:
    relay = kwargs.pop("relay", None) or _FakeRelay()
    return _coordinator(relay=relay, **kwargs), relay


# ── 1. The coordinator write paths ────────────────────────────────────


def test_a_number_write_reaches_the_relay_as_number_set() -> None:
    coordinator, relay = _enabled_coordinator()

    asyncio.run(
        coordinator.async_set_virtual_number(OWNED_ID, "number:200", 22.5)
    )

    assert relay.sent == [(OWNED_ID, "Number.Set", {"id": 200, "value": 22.5})]
    assert coordinator.refreshes == [1], "the state comes from the poll, not from us"


def test_a_text_write_reaches_the_relay_as_text_set() -> None:
    coordinator, relay = _enabled_coordinator()

    asyncio.run(
        coordinator.async_set_virtual_text(OWNED_ID, "text:200", "Wohnzimmer Ost")
    )

    assert relay.sent == [
        (OWNED_ID, "Text.Set", {"id": 200, "value": "Wohnzimmer Ost"})
    ]
    assert coordinator.refreshes == [1]


def test_an_enum_write_reaches_the_relay_as_enum_set() -> None:
    coordinator, relay = _enabled_coordinator()

    asyncio.run(coordinator.async_set_virtual_enum(OWNED_ID, "enum:200", "boost"))

    assert relay.sent == [(OWNED_ID, "Enum.Set", {"id": 200, "value": "boost"})]
    assert coordinator.refreshes == [1]


def test_a_script_is_started_and_stopped_by_two_different_methods() -> None:
    """Measured: ``Script.Start`` / ``Script.Stop``, not a config write.

    ``Script.SetConfig{enable}`` also works over the relay, but ``enable`` is
    the autostart flag and the cloud status reports ``running``. An entity
    whose command and whose state are different fields cannot be confirmed by
    the poll, so it is not built.
    """
    coordinator, relay = _enabled_coordinator()

    asyncio.run(coordinator.async_set_script_running(OWNED_ID, "script:2", True))
    asyncio.run(coordinator.async_set_script_running(OWNED_ID, "script:2", False))

    assert relay.sent == [
        (OWNED_ID, "Script.Start", {"id": 2}),
        (OWNED_ID, "Script.Stop", {"id": 2}),
    ]
    assert coordinator.refreshes == [1, 1]


@pytest.mark.parametrize(
    ("method", "args"),
    [
        ("async_set_virtual_number", ("number:200", 1.0)),
        ("async_set_virtual_text", ("text:200", "x")),
        ("async_set_virtual_enum", ("enum:200", "eco")),
        ("async_set_script_running", ("script:2", True)),
    ],
)
def test_every_write_raises_when_the_relay_refuses(method: str, args: tuple) -> None:
    """A swallowed error looks exactly like a component that did not move."""
    coordinator, _relay = _enabled_coordinator(
        relay=_FakeRelay(
            command_error=ShellyCloudWsCommandError("refused", code="WRONG_ID")
        )
    )

    with pytest.raises(ShellyCloudWsCommandError):
        asyncio.run(getattr(coordinator, method)(OWNED_ID, *args))
    assert coordinator.refreshes == [], "a failed command confirms nothing"


@pytest.mark.parametrize(
    ("method", "args"),
    [
        ("async_set_virtual_number", ("number:200", 1.0)),
        ("async_set_virtual_text", ("text:200", "x")),
        ("async_set_virtual_enum", ("enum:200", "eco")),
        ("async_set_script_running", ("script:2", True)),
    ],
)
def test_every_write_raises_without_a_connection(method: str, args: tuple) -> None:
    coordinator = _coordinator(relay=None)

    with pytest.raises(HomeAssistantError):
        asyncio.run(getattr(coordinator, method)(OWNED_ID, *args))


@pytest.mark.parametrize(
    ("method", "args"),
    [
        ("async_set_virtual_number", ("number:200", 1.0)),
        ("async_set_virtual_text", ("text:200", "x")),
        ("async_set_virtual_enum", ("enum:200", "eco")),
        ("async_set_script_running", ("script:2", True)),
    ],
)
def test_every_write_raises_for_a_device_the_relay_will_not_route_to(
    method: str, args: tuple
) -> None:
    coordinator, _relay = _enabled_coordinator(
        ownership=DeviceOwnership.NOT_ROUTABLE, device_id=SHARED_ID
    )

    with pytest.raises(HomeAssistantError):
        asyncio.run(getattr(coordinator, method)(SHARED_ID, *args))


@pytest.mark.parametrize(
    ("method", "key"),
    [
        ("async_set_virtual_number", "boolean:200"),
        ("async_set_virtual_text", "number:200"),
        ("async_set_virtual_enum", "text:200"),
        ("async_set_script_running", "enum:200"),
    ],
)
def test_a_write_to_the_wrong_component_type_raises(method: str, key: str) -> None:
    """The id is parsed out of the key, so the key has to be the right kind."""
    coordinator, relay = _enabled_coordinator()

    value: Any = True if method == "async_set_script_running" else "x"
    if method == "async_set_virtual_number":
        value = 1.0

    with pytest.raises(HomeAssistantError):
        asyncio.run(getattr(coordinator, method)(OWNED_ID, key, value))
    assert relay.sent == []


# ── 2. Which devices get probed and which keys are offered ────────────


def test_a_device_whose_only_writable_component_is_a_number_is_probed() -> None:
    """Before #48 the probe list only looked for booleans and BLU TRVs."""
    coordinator = _coordinator(
        status={"sys": {"mac": "AA"}, "switch:0": {"output": False},
                "number:200": {"value": 1}},
    )
    coordinator.is_enabled = lambda device_id: True

    assert coordinator._control_candidates() == [OWNED_ID]


def test_a_device_whose_only_writable_component_is_a_script_is_probed() -> None:
    coordinator = _coordinator(
        status={"sys": {"mac": "AA"}, "switch:0": {"output": False},
                "script:1": {"id": 1, "running": True}},
    )
    coordinator.is_enabled = lambda device_id: True

    assert coordinator._control_candidates() == [OWNED_ID]


def test_the_component_key_readers_return_only_their_own_kind() -> None:
    coordinator = _coordinator()

    assert coordinator.cloud_control_number_keys(OWNED_ID) == ["number:200"]
    assert coordinator.cloud_control_text_keys(OWNED_ID) == ["text:200"]
    assert coordinator.cloud_control_enum_keys(OWNED_ID) == ["enum:200"]
    assert coordinator.cloud_control_script_keys(OWNED_ID) == ["script:1", "script:2"]
    assert coordinator.cloud_control_boolean_keys(OWNED_ID) == ["boolean:200"]


# ── 3. The number entity ──────────────────────────────────────────────


def _number(configs: dict | None = RIG_CONFIG, **kwargs: Any):
    coordinator, relay = _enabled_coordinator(configs=configs, **kwargs)
    return number_platform.ShellyVirtualNumber(
        coordinator, OWNED_ID, "number:200"
    ), coordinator, relay


def test_the_number_takes_its_range_unit_and_name_from_the_measured_config() -> None:
    entity, _c, _r = _number()

    assert entity.name == "DIY Number Probe"
    assert entity.native_min_value == 4
    assert entity.native_max_value == 30
    assert entity.native_step == 0.5
    assert entity.native_unit_of_measurement == "°C"
    assert entity.native_value == 22.5
    assert entity.unique_id == f"{OWNED_ID}_number:200_control"


def test_a_number_without_config_stays_usable_instead_of_clamping_to_100() -> None:
    """The config arrives after the entity does — and may never arrive.

    Home Assistant's default range is 0–100. A virtual number holding 5000
    would be rejected against that, so an unknown range must be wide, not
    default.
    """
    entity, _c, _r = _number(configs=None)

    assert entity.name == "Number 200"
    assert entity.native_min_value < -1e6
    assert entity.native_max_value > 1e6
    assert entity.native_unit_of_measurement is None


def test_setting_the_number_drives_the_coordinator() -> None:
    entity, _c, relay = _number()

    asyncio.run(entity.async_set_native_value(24.0))

    assert relay.sent == [(OWNED_ID, "Number.Set", {"id": 200, "value": 24.0})]


# ── 4. The select entity ──────────────────────────────────────────────


def _select(configs: dict | None = RIG_CONFIG, status: dict | None = None, **kw: Any):
    coordinator, relay = _enabled_coordinator(configs=configs, status=status, **kw)
    return select_platform.ShellyVirtualSelect(
        coordinator, OWNED_ID, "enum:200"
    ), coordinator, relay


def test_the_select_offers_the_configured_options() -> None:
    entity, _c, _r = _select()

    assert entity.name == "DIY Enum Probe"
    assert entity.options == ["eco", "comfort", "boost"]
    assert entity.current_option == "comfort"
    assert entity.extra_state_attributes == {
        "titles": {"eco": "Eco Mode", "comfort": "Comfort", "boost": "Boost"}
    }


def test_a_value_outside_the_configured_options_is_shown_not_dropped() -> None:
    """Measured refusal: the device rejects a value that is not an option…

    …but the *config* is a cached copy that can lag behind a component someone
    reconfigured. Home Assistant logs an error for a ``current_option`` that is
    not in ``options``, so the live value is added rather than hidden — an
    entity showing the truth beats one showing ``unknown``.
    """
    status = dict(RIG_STATUS) | {"enum:200": {"value": "party", "source": "rpc"}}
    entity, _c, _r = _select(status=status)

    assert entity.current_option == "party"
    assert entity.options == ["eco", "comfort", "boost", "party"]


def test_a_select_without_config_falls_back_to_the_live_value() -> None:
    entity, _c, _r = _select(configs=None)

    assert entity.name == "Enum 200"
    assert entity.options == ["comfort"]
    assert entity.current_option == "comfort"


def test_choosing_an_option_drives_the_coordinator() -> None:
    entity, _c, relay = _select()

    asyncio.run(entity.async_select_option("boost"))

    assert relay.sent == [(OWNED_ID, "Enum.Set", {"id": 200, "value": "boost"})]


# ── 5. The text entity ────────────────────────────────────────────────


def _text(configs: dict | None = RIG_CONFIG, **kw: Any):
    coordinator, relay = _enabled_coordinator(configs=configs, **kw)
    return text_platform.ShellyVirtualText(
        coordinator, OWNED_ID, "text:200"
    ), coordinator, relay


def test_the_text_takes_its_length_and_name_from_the_measured_config() -> None:
    entity, _c, _r = _text()

    assert entity.name == "DIY Text Probe"
    assert entity.native_max == 255
    assert entity.native_value == "Wohnzimmer Ost"


def test_setting_the_text_drives_the_coordinator() -> None:
    entity, _c, relay = _text()

    asyncio.run(entity.async_set_value("Küche"))

    assert relay.sent == [(OWNED_ID, "Text.Set", {"id": 200, "value": "Küche"})]


# ── 6. Scripts ────────────────────────────────────────────────────────


def test_a_script_becomes_a_read_only_running_sensor_for_everyone() -> None:
    """The sensor does not depend on cloud control — the poll carries it."""
    coordinator = _coordinator(configs=RIG_CONFIG)
    sensors = create_rpc_binary_sensors(OWNED_ID, RIG_STATUS, set(), coordinator)
    scripts = [e for e in sensors if isinstance(e, ShellyScriptBinarySensor)]

    by_uid = {e.unique_id: e for e in scripts}
    assert sorted(by_uid) == [
        f"{OWNED_ID}_script:1_running",
        f"{OWNED_ID}_script:2_running",
    ]
    running = by_uid[f"{OWNED_ID}_script:1_running"]
    assert running.is_on is True
    assert running.name == "aioshelly_ble_integration"
    assert by_uid[f"{OWNED_ID}_script:2_running"].is_on is False


def test_a_script_without_a_running_field_creates_nothing() -> None:
    """``running`` is the only field every measured script carried.

    ``errors`` appeared on one script and not on the other in the same
    response, which is exactly why nothing is built on it.
    """
    status = {"sys": {"mac": "AA"}, "switch:0": {"output": False},
              "script:9": {"id": 9, "mem_used": 12}}
    coordinator = _coordinator(status=status)
    sensors = create_rpc_binary_sensors(OWNED_ID, status, set(), coordinator)

    assert [e for e in sensors if isinstance(e, ShellyScriptBinarySensor)] == []


def test_the_script_switch_reads_running_and_writes_start_stop() -> None:
    coordinator, relay = _enabled_coordinator(configs=RIG_CONFIG)
    entity = switch_platform.ShellyScriptSwitch(coordinator, OWNED_ID, "script:2")

    assert entity.unique_id == f"{OWNED_ID}_script:2_control"
    assert entity.name == "diy_probe_dummy"
    assert entity.is_on is False

    asyncio.run(entity.async_turn_on())
    asyncio.run(entity.async_turn_off())

    assert relay.sent == [
        (OWNED_ID, "Script.Start", {"id": 2}),
        (OWNED_ID, "Script.Stop", {"id": 2}),
    ]


# ── 7. The promises that hold across all four ─────────────────────────


def test_control_entities_do_not_replace_the_read_only_sensors() -> None:
    """Replacing them would strand every automation pointing at the sensor."""
    coordinator = _coordinator(configs=RIG_CONFIG)
    sensors = create_rpc_sensors(OWNED_ID, RIG_STATUS, set(), coordinator)
    virtual = {
        e.unique_id for e in sensors if isinstance(e, RpcVirtualSensor)
    }

    assert virtual == {
        f"{OWNED_ID}_number:200_value",
        f"{OWNED_ID}_text:200_value",
        f"{OWNED_ID}_enum:200_value",
    }
    # …and none of them collides with a control entity's id.
    control_ids = {
        number_platform.ShellyVirtualNumber(
            coordinator, OWNED_ID, "number:200").unique_id,
        text_platform.ShellyVirtualText(
            coordinator, OWNED_ID, "text:200").unique_id,
        select_platform.ShellyVirtualSelect(
            coordinator, OWNED_ID, "enum:200").unique_id,
    }
    assert virtual.isdisjoint(control_ids)


def test_no_control_entity_is_built_while_cloud_control_is_off() -> None:
    """Off means off: the default install must gain nothing from this."""
    coordinator = _coordinator(relay=None, options={}, configs=RIG_CONFIG)

    assert number_platform._controllable_keys(coordinator, OWNED_ID) == []
    assert text_platform._controllable_keys(coordinator, OWNED_ID) == []
    assert select_platform._controllable_keys(coordinator, OWNED_ID) == []
    assert switch_platform._controllable_script_keys(coordinator, OWNED_ID) == []


@pytest.mark.parametrize(
    ("factory", "key"),
    [
        (lambda c: number_platform.ShellyVirtualNumber(c, OWNED_ID, "number:200"), 0),
        (lambda c: text_platform.ShellyVirtualText(c, OWNED_ID, "text:200"), 0),
        (lambda c: select_platform.ShellyVirtualSelect(c, OWNED_ID, "enum:200"), 0),
        (lambda c: switch_platform.ShellyScriptSwitch(c, OWNED_ID, "script:2"), 0),
    ],
)
def test_a_control_entity_is_unavailable_while_the_relay_is_down(
    factory: Any, key: int
) -> None:
    """A control that renders operable while every press throws is worse."""
    coordinator, _relay = _enabled_coordinator(
        relay=_FakeRelay(connected=False), configs=RIG_CONFIG
    )
    entity = factory(coordinator)

    assert entity.available is False
