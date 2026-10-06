"""Unit tests for the BLU firmware version sensor (``fwversion:0.version``).

A Shelly BLU device reports its firmware as ``fwversion:0`` with
``{"id": 0, "version": "1.0.16"}``. In the recorded account snapshot the key
is on 29 of 29 gateway-bridged devices; the version reads 1.0.16 (20×),
1.0.19, 1.0.23, 1.0.9-rc3 (2×) — and is an empty string on 5. Nothing
surfaced it: the Firmware update flag reads ``sys``, which BLU devices do
not have, and the device registry carries no software version.

These tests drive the real creation loop (``_create_ble_sensors``), the real
entity and the real diagnostics coverage report with a lightweight fake
coordinator, so they run identically against any HA version and need no
running Home Assistant.
"""
from __future__ import annotations

from typing import Any

from homeassistant.const import EntityCategory

from custom_components.shelly_cloud_diy.diagnostics import _coverage_diagnostics
from custom_components.shelly_cloud_diy.sensor import (
    BleFirmwareVersionSensor,
    _create_ble_sensors,
    _usable_version,
)

DEVICE_ID = "XB106582483818186"
UID = f"{DEVICE_ID}_firmware_version"


class _FakeCoordinator:
    """Minimal coordinator: entities only read ``.devices[id]['status']``."""

    def __init__(self, device_id: str, status: dict[str, Any]) -> None:
        self.devices = {
            device_id: {
                "status": status,
                "device_code": "SBMO-003Z",
                "online": True,
            }
        }
        self.data = self.devices
        self.last_update_success = True


def _blu_status(fwversion: Any = "default") -> dict[str, Any]:
    """The shape a BLU Motion arrives in, ``fwversion:0`` included."""
    status: dict[str, Any] = {
        "_dev_info": {"gen": "GBLE", "code": "SBMO-003Z"},
        "illuminance:0": {"id": 0, "lux": 12},
        "motion:0": {"id": 0, "motion": False},
        "devicepower:0": {"id": 0, "battery": {"percent": 90, "V": None}},
    }
    if fwversion == "default":
        fwversion = {"id": 0, "version": "1.0.16"}
    if fwversion is not None:
        status["fwversion:0"] = fwversion
    return status


def _build(status: dict[str, Any], created: set[str] | None = None):
    coord = _FakeCoordinator(DEVICE_ID, status)
    entities = _create_ble_sensors(
        DEVICE_ID, status, set() if created is None else created, coord
    )
    return entities, {e.unique_id: e for e in entities}


def test_firmware_version_sensor_is_created_and_reads_the_version() -> None:
    """The version the 24 measured BLU devices carry becomes an entity."""
    _, by_uid = _build(_blu_status())

    sensor = by_uid[UID]
    assert isinstance(sensor, BleFirmwareVersionSensor)
    assert sensor.native_value == "1.0.16"
    assert sensor.entity_category is EntityCategory.DIAGNOSTIC
    assert sensor.device_class is None
    assert sensor.state_class is None
    assert sensor.native_unit_of_measurement is None
    assert sensor.entity_registry_enabled_default is True
    assert sensor.name == "Firmware version"


def test_a_release_candidate_is_reported_verbatim() -> None:
    """Two measured devices run 1.0.9-rc3 — that suffix is the point."""
    _, by_uid = _build(_blu_status({"id": 0, "version": "1.0.9-rc3"}))

    assert by_uid[UID].native_value == "1.0.9-rc3"


def test_no_entity_without_a_usable_version() -> None:
    """The gate is the value, not the key.

    5 of the 29 measured devices carry the key with an empty version, and an
    entity that can only ever say "unknown" is worse than none.
    """
    for fwversion in (
        None,
        {},
        {"id": 0},
        {"id": 0, "version": ""},
        {"id": 0, "version": "   "},
        {"id": 0, "version": None},
        {"id": 0, "version": 16},
        {"id": 0, "version": True},
        "1.0.16",
    ):
        _, by_uid = _build(_blu_status(fwversion))
        assert UID not in by_uid, f"entity built for fwversion={fwversion!r}"


def test_a_device_without_a_version_still_gets_its_other_sensors() -> None:
    """A missing version must cost exactly one entity, not the rest."""
    _, by_uid = _build(_blu_status({"id": 0, "version": ""}))

    assert UID not in by_uid
    assert f"{DEVICE_ID}_ble_illuminance_0" in by_uid
    assert f"{DEVICE_ID}_ble_battery_percent" in by_uid


def test_the_value_tracks_the_poll() -> None:
    """A firmware update shows up on the next poll, a blank one as unknown."""
    status = _blu_status()
    _, by_uid = _build(status)
    sensor = by_uid[UID]

    status["fwversion:0"]["version"] = "1.0.23"
    assert sensor.native_value == "1.0.23"

    status["fwversion:0"]["version"] = ""
    assert sensor.native_value is None

    del status["fwversion:0"]
    assert sensor.native_value is None


def test_the_sensor_is_created_once_per_device() -> None:
    """The ``created`` set is what stops a rediscovery duplicating entities."""
    created: set[str] = set()
    _build(_blu_status(), created)
    _, by_uid = _build(_blu_status(), created)

    assert UID not in by_uid


def test_the_unique_id_does_not_collide_with_the_firmware_update_flag() -> None:
    """Both live on the same device page; their IDs must stay distinct."""
    assert UID != f"{DEVICE_ID}_firmware_update"


def test_the_coverage_report_counts_fwversion_only_while_it_reads() -> None:
    """``_status_key`` is how the diagnostics report learns about this key.

    Covered while a version is reported; back to a gap when it is blank,
    because then no entity is built — exactly what the report must say.
    """
    status = _blu_status()
    report = _coverage_diagnostics(
        _FakeCoordinator(DEVICE_ID, status), DEVICE_ID, status
    )
    assert "fwversion:0" in report["covered_keys"]
    assert "fwversion:0" not in report["uncovered_keys"]

    status["fwversion:0"]["version"] = ""
    report = _coverage_diagnostics(
        _FakeCoordinator(DEVICE_ID, status), DEVICE_ID, status
    )
    assert "fwversion:0" in report["uncovered_keys"]


def test_usable_version_rejects_everything_that_is_not_a_version() -> None:
    """The shared filter, pinned on its own."""
    assert _usable_version("1.0.16") == "1.0.16"
    assert _usable_version(" 1.0.19 ") == "1.0.19"
    assert _usable_version("") is None
    assert _usable_version("  ") is None
    assert _usable_version(None) is None
    assert _usable_version(16) is None
    assert _usable_version(True) is None
