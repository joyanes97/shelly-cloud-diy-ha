"""A 401 is not proof that the key is dead (#50).

Reported by @Frido1980: Home Assistant suddenly demanded re-authentication on
an account whose password had never changed, and whose Authorization cloud key
had therefore never been regenerated. There is a second, independent case of
the same thing on the maintainer's own account. Neither cause was established.

What the code did was treat **any** 401/403 that is not Shelly's ``max_req``
rate-limit body as "the stored key is invalid", immediately and on the first
occurrence. Shelly answers 401 for at least three different situations and
only labels one of them, so that inference was stronger than the evidence —
and the cost of being wrong is high: the integration stops and stays stopped
until a human pastes a key, even if the next poll would have succeeded.

The rule here is the honest version of it:

* Shelly **says** the key is invalid (``invalid_auth_key`` in the body) →
  re-authentication, immediately, as before. That is evidence.
* An unexplained 401/403 → an ordinary failed poll, retried like any other,
  and only escalated to re-authentication once it has **persisted**. A cloud
  having a bad minute must not cost the user a manual recovery.

Everything the refusal said is kept for diagnostics, because "we could not
tell why" is only acceptable if the next report can.
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.shelly_cloud_diy import coordinator as coordinator_module
from custom_components.shelly_cloud_diy.api import cloud_control
from custom_components.shelly_cloud_diy.api.cloud_control import (
    ShellyCloudAuthError,
    ShellyCloudControl,
    ShellyCloudRateLimitError,
)
from custom_components.shelly_cloud_diy.coordinator import ShellyCloudCoordinator

FAKE_KEY = "FAKE-AUTH-KEY-not-a-real-credential-0001"  # noqa: S105
SERVER = "https://shelly-42-eu.shelly.cloud"

# The three bodies Shelly is known to answer 401 with. Only the middle one
# names the credential; the first is the documented rate limit, and the last
# is what the reporter's account produced — a refusal that explains nothing.
RATE_LIMIT_BODY = '{"isok":false,"errors":{"max_req":"Request limit reached!"}}'
INVALID_KEY_BODY = '{"isok":false,"errors":{"invalid_auth_key":"Wrong auth key"}}'
UNEXPLAINED_BODY = '{"isok":false,"errors":{}}'


# ── Harness ───────────────────────────────────────────────────────────


class _Response:
    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self._body = body

    async def text(self) -> str:
        return self._body

    async def json(self, content_type: Any = None) -> Any:
        import json

        return json.loads(self._body)

    def raise_for_status(self) -> None:
        return None

    async def __aenter__(self) -> _Response:
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None


class _Session:
    """Answers every POST with the next queued response."""

    def __init__(self, *responses: _Response) -> None:
        self._responses = list(responses)
        self.calls = 0

    def post(self, *_: Any, **__: Any) -> _Response:
        self.calls += 1
        return self._responses[min(self.calls - 1, len(self._responses) - 1)]


def _api(*responses: _Response) -> ShellyCloudControl:
    return ShellyCloudControl(_Session(*responses), SERVER, FAKE_KEY)


# ── 1. What the API layer concludes from a refusal ────────────────────


def test_a_refusal_that_names_the_key_is_treated_as_proof() -> None:
    with pytest.raises(ShellyCloudAuthError) as excinfo:
        asyncio.run(_api(_Response(401, INVALID_KEY_BODY)).get_all_status())

    err = excinfo.value
    assert err.confirmed is True, "Shelly named the credential; that is evidence"
    assert err.status == 401


def test_an_unexplained_refusal_is_not_treated_as_proof() -> None:
    with pytest.raises(ShellyCloudAuthError) as excinfo:
        asyncio.run(_api(_Response(401, UNEXPLAINED_BODY)).get_all_status())

    err = excinfo.value
    assert err.confirmed is False
    assert err.status == 401
    assert err.detail, "the refusal has to survive for the next bug report"


def test_a_403_is_as_ambiguous_as_a_401() -> None:
    with pytest.raises(ShellyCloudAuthError) as excinfo:
        asyncio.run(_api(_Response(403, "forbidden")).get_all_status())
    assert excinfo.value.confirmed is False
    assert excinfo.value.status == 403


def test_the_rate_limit_body_is_still_not_an_auth_error_at_all(monkeypatch) -> None:
    """The one 401 that was already understood must not regress."""
    monkeypatch.setattr(cloud_control, "_RATE_LIMIT_BACKOFF_S", 0)
    with pytest.raises(ShellyCloudRateLimitError):
        asyncio.run(
            _api(
                _Response(401, RATE_LIMIT_BODY), _Response(401, RATE_LIMIT_BODY)
            ).get_all_status()
        )


def test_the_kept_refusal_never_carries_the_key() -> None:
    """The body is remote text on its way into a diagnostics download."""
    echoed = f'{{"isok":false,"errors":{{"bad":"key {FAKE_KEY} rejected"}}}}'
    with pytest.raises(ShellyCloudAuthError) as excinfo:
        asyncio.run(_api(_Response(401, echoed)).get_all_status())

    detail = excinfo.value.detail or ""
    assert FAKE_KEY not in detail
    assert "redacted" in detail.lower()


def test_the_kept_refusal_is_capped() -> None:
    with pytest.raises(ShellyCloudAuthError) as excinfo:
        asyncio.run(_api(_Response(401, "x" * 2000)).get_all_status())
    assert len(excinfo.value.detail or "") <= 256


# ── 2. What the coordinator does with it ──────────────────────────────


def _coordinator() -> ShellyCloudCoordinator:
    coordinator = object.__new__(ShellyCloudCoordinator)
    coordinator._entry = SimpleNamespace(entry_id="e1", options={}, data={})
    coordinator.hass = SimpleNamespace(data={})
    coordinator.devices = {}
    coordinator.data = {}
    coordinator._ambiguous_auth_streak = 0
    coordinator._ambiguous_auth_since = None
    coordinator.last_auth_failure = None
    return coordinator


def _raise(coordinator: ShellyCloudCoordinator, err: ShellyCloudAuthError, now: float):
    """Run the decision the poll makes about one auth failure."""
    return coordinator._auth_failure_outcome(err, now)


def test_a_confirmed_rejection_asks_for_a_new_key_at_once() -> None:
    coordinator = _coordinator()
    err = ShellyCloudAuthError("rejected", confirmed=True, status=401)

    assert isinstance(_raise(coordinator, err, 0.0), ConfigEntryAuthFailed)


def test_an_unexplained_refusal_is_first_just_a_failed_poll() -> None:
    coordinator = _coordinator()
    err = ShellyCloudAuthError("huh", confirmed=False, status=401)

    assert isinstance(_raise(coordinator, err, 0.0), UpdateFailed)
    assert isinstance(_raise(coordinator, err, 5.0), UpdateFailed)
    assert isinstance(_raise(coordinator, err, 10.0), UpdateFailed)


def test_an_unexplained_refusal_that_persists_does_become_a_reauth() -> None:
    """Both gates: enough tries AND enough time. A fast poll is not evidence."""
    coordinator = _coordinator()
    err = ShellyCloudAuthError("huh", confirmed=False, status=401)

    for moment in (0.0, 5.0, 10.0, 15.0):
        assert isinstance(_raise(coordinator, err, moment), UpdateFailed)

    late = coordinator_module.AMBIGUOUS_AUTH_MIN_SECONDS + 1
    assert isinstance(_raise(coordinator, err, late), ConfigEntryAuthFailed)


def test_time_alone_does_not_escalate() -> None:
    """One lonely 401 an hour ago is not a sustained condition."""
    coordinator = _coordinator()
    err = ShellyCloudAuthError("huh", confirmed=False, status=401)

    _raise(coordinator, err, 0.0)
    assert isinstance(
        _raise(coordinator, err, coordinator_module.AMBIGUOUS_AUTH_MIN_SECONDS + 1),
        UpdateFailed,
    ), "two attempts are not a streak, however far apart they are"


def test_one_good_poll_clears_the_streak() -> None:
    """Exactly the case this exists for: the cloud had a bad minute."""
    coordinator = _coordinator()
    err = ShellyCloudAuthError("huh", confirmed=False, status=401)

    for moment in (0.0, 5.0, 10.0, 15.0):
        _raise(coordinator, err, moment)
    coordinator._note_auth_ok()

    late = coordinator_module.AMBIGUOUS_AUTH_MIN_SECONDS + 100
    assert isinstance(_raise(coordinator, err, late), UpdateFailed)


def test_what_the_refusal_said_is_kept_for_the_bug_report() -> None:
    coordinator = _coordinator()
    err = ShellyCloudAuthError("huh", confirmed=False, status=403, detail="nope")

    _raise(coordinator, err, 0.0)

    assert coordinator.last_auth_failure == {
        "status": 403,
        "confirmed": False,
        "detail": "nope",
        "consecutive": 1,
    }


# ── 3. The dialog the user lands in ───────────────────────────────────


FAKE_NEW_KEY = "FAKE-REPLACEMENT-KEY-not-a-credential"  # noqa: S105
OTHER_SERVER = "https://shelly-77-eu.shelly.cloud"


class _FlowEntry:
    def __init__(self) -> None:
        self.entry_id = "e1"
        self.data = {"auth_key": "NOTAREALKEY-shouldneverappear",
                     "server_uri": SERVER}
        self.options: dict[str, Any] = {}


def _flow(entry: _FlowEntry, monkeypatch, *, accepts: bool = True):
    from custom_components.shelly_cloud_diy import config_flow

    class _Api:
        def __init__(self, session, server_uri, auth_key) -> None:
            self.server_uri = server_uri
            self.auth_key = auth_key
            seen.append((server_uri, auth_key))

        async def validate(self) -> int:
            if not accepts:
                raise ShellyCloudAuthError("no", confirmed=True, status=401)
            return 3

    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(config_flow, "ShellyCloudControl", _Api)
    monkeypatch.setattr(config_flow, "async_get_clientsession", lambda hass: None)

    reloads: list[str] = []

    def _update(config_entry, **kwargs):
        if "data" in kwargs:
            config_entry.data = dict(kwargs["data"])

    async def _reload(entry_id):
        reloads.append(entry_id)

    flow = config_flow.ShellyCloudDiyConfigFlow()
    flow.hass = SimpleNamespace(
        config_entries=SimpleNamespace(
            async_get_entry=lambda entry_id: entry,
            async_update_entry=_update,
            async_reload=_reload,
        )
    )
    flow.context = {"entry_id": entry.entry_id}
    return flow, seen, reloads


def test_the_reauth_dialog_also_offers_the_server_uri(monkeypatch) -> None:
    """Shelly documents that an account can be moved between servers.

    The dialog asked only for the key and silently reused the stored URI, so
    a migrated account could not be repaired through it at all. (#50)
    """
    entry = _FlowEntry()
    flow, _seen, _reloads = _flow(entry, monkeypatch)

    form = asyncio.run(flow.async_step_reauth_confirm())

    fields = {str(key) for key in form["data_schema"].schema}
    assert fields == {"auth_key", "server_uri"}


def test_a_changed_server_uri_is_validated_and_stored(monkeypatch) -> None:
    entry = _FlowEntry()
    flow, seen, reloads = _flow(entry, monkeypatch)

    result = asyncio.run(
        flow.async_step_reauth_confirm(
            {"auth_key": FAKE_NEW_KEY, "server_uri": OTHER_SERVER}
        )
    )

    assert result["reason"] == "reauth_successful"
    assert seen == [(OTHER_SERVER, FAKE_NEW_KEY)], "the pair is checked together"
    assert entry.data["server_uri"] == OTHER_SERVER
    assert entry.data["auth_key"] == FAKE_NEW_KEY
    assert reloads == ["e1"]


def test_an_empty_server_uri_falls_back_to_the_stored_one(monkeypatch) -> None:
    """Most users only need to paste a key; the URI must not become a chore."""
    entry = _FlowEntry()
    flow, seen, _reloads = _flow(entry, monkeypatch)

    asyncio.run(flow.async_step_reauth_confirm({"auth_key": FAKE_NEW_KEY}))

    assert seen == [(SERVER, FAKE_NEW_KEY)]


def test_a_rejected_pair_leaves_the_entry_untouched(monkeypatch) -> None:
    entry = _FlowEntry()
    before = dict(entry.data)
    flow, _seen, reloads = _flow(entry, monkeypatch, accepts=False)

    form = asyncio.run(
        flow.async_step_reauth_confirm(
            {"auth_key": FAKE_NEW_KEY, "server_uri": OTHER_SERVER}
        )
    )

    assert form["errors"] == {"base": "invalid_auth"}
    assert entry.data == before
    assert reloads == []


# ── 4. What a bug report will contain ─────────────────────────────────


def test_diagnostics_report_the_last_refusal_without_the_key() -> None:
    from custom_components.shelly_cloud_diy import diagnostics

    coordinator = _coordinator()
    coordinator.last_auth_failure = {
        "status": 401,
        "confirmed": False,
        "detail": '{"isok":false,"errors":{}}',
        "consecutive": 2,
    }

    block = diagnostics._auth_failure_diagnostics(coordinator)

    assert block == coordinator.last_auth_failure
    assert diagnostics._auth_failure_diagnostics(SimpleNamespace()) is None
