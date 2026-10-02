"""Guards on ``manifest.json`` that CI only catches a day later.

The nightly hassfest job is the real check, but it runs on a schedule against
whatever hassfest has become — so a rule introduced upstream shows up as a
failed cron mail the next morning rather than on the push that broke it. The
one rule this repo has actually been caught by is kept here too, where it
fails in the same second as the change.
"""
from __future__ import annotations

import json
import pathlib

MANIFEST = json.loads(
    (
        pathlib.Path(__file__).parent.parent
        / "custom_components/shelly_cloud_diy/manifest.json"
    ).read_text()
)

# Shipped by Home Assistant itself. hassfest rejects a custom integration that
# lists any of them, because the version it installs would fight the one HA
# already has. ``aiohttp`` was listed here until 2026-10-01, when hassfest
# started enforcing it and the nightly job went red.
HOME_ASSISTANT_OWN_REQUIREMENTS = {
    "aiohttp", "async-timeout", "attrs", "awesomeversion", "certifi",
    "ciso8601", "cryptography", "jinja2", "orjson", "pyyaml", "requests",
    "urllib3", "voluptuous", "yarl",
}


def _requirement_name(requirement: str) -> str:
    for separator in ("==", ">=", "<=", "~=", ">", "<", "!=", "["):
        requirement = requirement.split(separator, 1)[0]
    return requirement.strip().lower()


def test_no_requirement_is_one_home_assistant_already_ships() -> None:
    listed = {_requirement_name(r) for r in MANIFEST.get("requirements", [])}
    assert not (listed & HOME_ASSISTANT_OWN_REQUIREMENTS), (
        "hassfest refuses these; import them, do not require them"
    )


def test_every_requirement_is_actually_imported() -> None:
    """A requirement nobody imports is a download on every user's install."""
    sources = (
        pathlib.Path(__file__).parent.parent / "custom_components/shelly_cloud_diy"
    ).rglob("*.py")
    code = "\n".join(path.read_text() for path in sources)
    for requirement in MANIFEST.get("requirements", []):
        name = _requirement_name(requirement).replace("-", "_")
        assert f"import {name}" in code or f"from {name}" in code, requirement
