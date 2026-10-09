"""A temperature below absolute zero is no reading, on every path. (#51)

Measured: an ECOWITT WS90 behind a BLU gateway reported ``tC: -999`` twice,
both times right after a Home Assistant restart (2026-10-06 12:07:41 and
2026-10-08 14:21:42 UTC), with a real value 5–10 s later. The integration
passed it through, so -999 landed in the state history and dragged that
hour's long-term statistics (min/mean) down with it.

The rule is the physical limit, not a list of sentinels: -999 is the only
placeholder ever observed, and any other value would be a guess. These tests
drive the real builders and the real entities with a fake coordinator, so
every temperature the integration exposes is checked the way a user would
meet it — not just the one path the report came from.
"""
from __future__ import annotations

from typing import Any, Callable

import pytest
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import UnitOfTemperature

from custom_components.shelly_cloud_diy import climate as climate_platform
from custom_components.shelly_cloud_diy.const import plausible_temperature
from custom_components.shelly_cloud_diy.device_health import evaluate_device_health
from custom_components.shelly_cloud_diy.sensor import (
    _create_ble_sensors,
    _create_block_sensors,
    _create_rpc_sensors,
)

DEVICE_ID = "XB277409423391452"

# The placeholder exactly as the cloud sent it, a real winter reading, and
# the last value on either side of the line.
SENTINEL = -999
REAL_FROST = -20.5
ABSOLUTE_ZERO_C = -273.15
JUST_BELOW_C = -273.16


class _FakeCoordinator:
    """Minimal coordinator: entities only read ``.devices[id]['status']``."""

    def __init__(self, status: dict[str, Any]) -> None:
        self.devices = {DEVICE_ID: {"status": status, "online": True}}
        self.data = self.devices
        self.last_update_success = True
        self.virtual_configs: dict[str, dict[str, dict]] = {}

    def is_enabled(self, device_id: str) -> bool:
        return True


# ── One status factory per path ───────────────────────────────────────
#
# Each returns the device status with ``value`` in the temperature field, or
# with the field absent when ``value`` is ``_MISSING``.

_MISSING = object()


def _put(payload: dict[str, Any], field: str, value: Any) -> dict[str, Any]:
    if value is not _MISSING:
        payload[field] = value
    return payload


def _ble_temperature(value: Any) -> dict[str, Any]:
    """The reported device: WS90 via BLU gateway, ``temperature:0.tC``."""
    return {
        "_dev_info": {"gen": "GBLE", "code": "SBWS-90CM"},
        "temperature:0": _put({"id": 0}, "tC", value),
        "humidity:0": {"id": 0, "rh": 81},
    }


def _ble_dewpoint(value: Any) -> dict[str, Any]:
    """Same device, the second temperature it reports: ``dewpoint:0.value``."""
    return {
        "_dev_info": {"gen": "GBLE", "code": "SBWS-90CM"},
        "dewpoint:0": _put({"id": 0}, "value", value),
    }


def _rpc_temperature(value: Any) -> dict[str, Any]:
    """A Gen2+ ``temperature:<id>`` component (H&T Gen3, add-on probe)."""
    return {"temperature:0": _put({"id": 0, "tF": 0.0}, "tC", value)}


def _rpc_switch_temperature(value: Any) -> dict[str, Any]:
    """A relay's own heat: ``switch:0.temperature.tC``."""
    return {
        "switch:0": {
            "id": 0,
            "output": False,
            "temperature": _put({"tF": 0.0}, "tC", value),
        }
    }


def _gen1_temperature(value: Any) -> dict[str, Any]:
    """A Gen1 device: ``tmp.tC``."""
    return {"tmp": _put({"tF": 0.0, "is_valid": True}, "tC", value)}


SENSOR_PATHS: dict[str, tuple[Callable[[Any], dict], Callable]] = {
    "ble temperature:0.tC": (_ble_temperature, _create_ble_sensors),
    "ble dewpoint:0.value": (_ble_dewpoint, _create_ble_sensors),
    "rpc temperature:0.tC": (_rpc_temperature, _create_rpc_sensors),
    "rpc switch:0.temperature.tC": (_rpc_switch_temperature, _create_rpc_sensors),
    "gen1 tmp.tC": (_gen1_temperature, _create_block_sensors),
}


def _temperature_sensor(path: str, value: Any):
    """Build the real entities for ``path`` and return its temperature one.

    The entity is built from a status that HAS the field (that is the only
    way it gets created) and then reads the status under test, the way a
    live entity sees the next poll.
    """
    make_status, build = SENSOR_PATHS[path]
    coordinator = _FakeCoordinator(make_status(18.3))
    entities = build(DEVICE_ID, coordinator.devices[DEVICE_ID]["status"], set(), coordinator)
    temperatures = [
        e for e in entities if e.device_class == SensorDeviceClass.TEMPERATURE
    ]
    assert len(temperatures) == 1, f"{path}: expected one temperature entity"
    sensor = temperatures[0]
    assert sensor.native_value == 18.3
    coordinator.devices[DEVICE_ID]["status"] = make_status(value)
    return sensor


# ── Every sensor path ─────────────────────────────────────────────────


@pytest.mark.parametrize("path", SENSOR_PATHS)
def test_the_measured_placeholder_is_no_reading(path: str) -> None:
    assert _temperature_sensor(path, SENTINEL).native_value is None


@pytest.mark.parametrize("path", SENSOR_PATHS)
def test_a_real_frost_reading_passes_unchanged(path: str) -> None:
    assert _temperature_sensor(path, REAL_FROST).native_value == REAL_FROST


@pytest.mark.parametrize("path", SENSOR_PATHS)
def test_absolute_zero_itself_is_the_line_and_passes(path: str) -> None:
    """Strictly below is rejected; the limit itself is not a margin."""
    assert _temperature_sensor(path, ABSOLUTE_ZERO_C).native_value == ABSOLUTE_ZERO_C
    assert _temperature_sensor(path, JUST_BELOW_C).native_value is None


@pytest.mark.parametrize("path", SENSOR_PATHS)
def test_a_null_or_missing_reading_stays_none(path: str) -> None:
    assert _temperature_sensor(path, None).native_value is None
    assert _temperature_sensor(path, _MISSING).native_value is None


def test_other_readings_of_the_same_device_are_not_judged() -> None:
    """-999 is only impossible as a temperature; a humidity is left alone."""
    coordinator = _FakeCoordinator(_ble_temperature(18.3))
    status = coordinator.devices[DEVICE_ID]["status"]
    entities = _create_ble_sensors(DEVICE_ID, status, set(), coordinator)
    humidity = next(e for e in entities if e.device_class == SensorDeviceClass.HUMIDITY)
    status["humidity:0"]["rh"] = SENTINEL
    assert humidity.native_value == SENTINEL


# ── BLU TRV climate ───────────────────────────────────────────────────


def _valve_climate(current_c: Any, target_c: Any = 10):
    status = {
        "sys": {"mac": "34987A1B2C3D"},
        "blutrv:200": {
            "id": 200,
            "current_C": current_c,
            "target_C": target_c,
            "pos": 0,
            "connected": True,
            "paired": True,
        },
    }
    coordinator = _FakeCoordinator(status)
    (entity,) = climate_platform._create_climate_entities(
        DEVICE_ID, status, set(), coordinator
    )
    return entity


def test_a_valve_placeholder_is_no_current_or_target_temperature() -> None:
    entity = _valve_climate(SENTINEL, SENTINEL)
    assert entity.current_temperature is None
    assert entity.target_temperature is None


def test_a_valve_reading_and_its_boundary() -> None:
    assert _valve_climate(REAL_FROST).current_temperature == REAL_FROST
    assert _valve_climate(ABSOLUTE_ZERO_C).current_temperature == ABSOLUTE_ZERO_C
    assert _valve_climate(JUST_BELOW_C).current_temperature is None
    assert _valve_climate(None).current_temperature is None


# ── Device health ─────────────────────────────────────────────────────


def test_device_health_neither_raises_nor_reports_on_the_placeholder() -> None:
    """The health check only fires at 70 °C and up, so -999 is silence.

    Pinned here so a later "too cold" check cannot start reading the
    placeholder as a frozen device.
    """
    status = {
        "switch:0": {"id": 0, "temperature": {"tC": SENTINEL, "tF": -1766.2}},
        "temperature:100": {"id": 100, "tC": SENTINEL},
    }
    assert evaluate_device_health(status) == []


# ── The helper itself ─────────────────────────────────────────────────


def test_the_limit_follows_the_unit() -> None:
    assert plausible_temperature(-459.67, UnitOfTemperature.FAHRENHEIT) == -459.67
    assert plausible_temperature(-459.68, UnitOfTemperature.FAHRENHEIT) is None
    assert plausible_temperature(0.0, UnitOfTemperature.KELVIN) == 0.0
    assert plausible_temperature(-0.01, UnitOfTemperature.KELVIN) is None
    # -300 °F is a real -184 °C reading in Fahrenheit, not below its limit.
    assert plausible_temperature(-300, UnitOfTemperature.FAHRENHEIT) == -300


def test_what_is_not_a_number_is_returned_unchanged() -> None:
    assert plausible_temperature(None) is None
    assert plausible_temperature(True) is True
    assert plausible_temperature("-999") == "-999"


def test_an_unknown_unit_has_no_limit_to_judge_by() -> None:
    assert plausible_temperature(SENTINEL, None) == SENTINEL
    assert plausible_temperature(SENTINEL, "furlongs") == SENTINEL
