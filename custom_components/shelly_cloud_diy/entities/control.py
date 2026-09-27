"""Shared behaviour of the entities that write over the cloud relay.

Four component kinds are written this way — virtual ``number``, ``text`` and
``enum``, and a ``script``'s running state — and the parts that must be
identical for all of them are the parts a user would notice if they were not:

* **The id.** ``<device>_<component key>_control``. Never the read-only
  sensor's ``…_value``: reusing that id would migrate an existing entity to
  another platform behind the user's back.
* **The name.** The same lazily fetched v2 config the sensor reads, so both
  entities of one component carry the same name — falling back to a generic
  one (``Number 200``) while the fetch is in flight or if it never resolves.
* **Availability.** The base class asks whether the *device* is reachable
  through the poll. A writing entity also needs the second connection, which
  can be down on its own, and a control that renders operable while every
  press is guaranteed to throw is the polite version of the failure this
  feature exists to prevent.
* **No optimism.** Nothing here writes a local value after a command. The
  poll is the single source of state, so "the device accepted it and did not
  move" stays visible instead of being painted over.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..coordinator import ShellyCloudCoordinator


def controllable_keys(
    coordinator: ShellyCloudCoordinator, device_id: str, reader: str
) -> list[str]:
    """Return the component keys a platform may build control entities for.

    Both questions go to the coordinator, and both go through ``getattr``.
    That is not defensive clutter but the platform's half of the opt-in
    contract: anything that is not a coordinator running with cloud control
    switched on answers "none", and the platform then builds nothing at all.
    Which components are writable is the coordinator's knowledge — it is the
    side that has to send the command.
    """
    controllable = getattr(coordinator, "is_cloud_controllable", None)
    keys = getattr(coordinator, reader, None)
    if not callable(controllable) or not callable(keys):
        return []
    return keys(device_id) if controllable(device_id) else []


class ShellyVirtualControlEntity:
    """Mixin for a component entity that writes over the cloud relay.

    Mixed in *before* :class:`~..entities.base.ShellyBaseEntity` so its
    ``available`` runs first and can add the relay's own state to the base
    class' answer.
    """

    #: Human-readable kind, used for the generic fallback name.
    _kind = "Component"

    def __init__(
        self,
        coordinator: ShellyCloudCoordinator,
        device_id: str,
        component_key: str,
    ) -> None:
        """Initialise the control entity for one component key."""
        super().__init__(coordinator, device_id, 0)
        self._component_key = component_key
        self._attr_unique_id = f"{device_id}_{component_key}_control"
        self._generic_name = f"{self._kind} {component_key.split(':', 1)[1]}"

    @property
    def name(self) -> str:
        """Resolved component name, or the generic fallback."""
        return self.virtual_component_name(self._component_key) or self._generic_name

    @property
    def available(self) -> bool:
        """Available only while the channel that carries the command is up."""
        return super().available and self.coordinator.cloud_control_connected

    def component(self) -> dict[str, Any]:
        """This component's raw status object, or an empty one."""
        component = self.device_status.get(self._component_key)
        return component if isinstance(component, dict) else {}

    def component_value(self) -> Any:
        """The component's ``value`` as the poll last saw it."""
        return self.component().get("value")
