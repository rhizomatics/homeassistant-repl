"""Tests for dev/events/scan.py, the event catalogue's scan, against a small
made-up Home Assistant source tree."""

from __future__ import annotations

import importlib.util
import json
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

_SCAN = Path(__file__).parent.parent / "dev" / "events" / "scan.py"
_spec = importlib.util.spec_from_file_location("event_scan", _SCAN)
assert _spec is not None and _spec.loader is not None
scan = importlib.util.module_from_spec(_spec)
sys.modules["event_scan"] = scan
_spec.loader.exec_module(scan)

SOURCE = {
    "const.py": """
        EVENT_STATE_CHANGED: EventType[Any] = EventType("state_changed")
        EVENT_CALL_SERVICE = "call_service"
    """,
    "core.py": """
        from .const import EVENT_CALL_SERVICE

        def call(hass):
            hass.bus.async_fire(EVENT_CALL_SERVICE, {})
    """,
    "components/__init__.py": "",
    "components/widget/manifest.json": '{"domain": "widget", "name": "Widget"}',
    "components/widget/__init__.py": "",
    "components/widget/const.py": """
        DOMAIN = "widget"
        EVENT_WIDGET_TURNED = f"{DOMAIN}_turned"
        EVENT_DATA_ANGLE = "angle"
        EVENT_WAIT = 5
        EVENT_UID = "uid"
        EVENT_WIDGET_PING = "widget_ping"
        EVENT_RIPPLE = "ripple_seen"
        EVENT_CLICK = "click_twice"
        EVENT_TIMER_DONE = "timer.done"
    """,
    "components/widget/hub.py": """
        from . import const
        from .const import EVENT_DATA_ANGLE, EVENT_TIMER_DONE

        def turned(hass, angle):
            hass.bus.async_fire(const.EVENT_WIDGET_TURNED, {EVENT_DATA_ANGLE: angle})
            hass.bus.async_fire("widget_literal", {})

        def finish(hass, which):
            event = {"done": EVENT_TIMER_DONE}[which]
            hass.bus.async_fire(event, {})
    """,
    "components/widget/event.py": """
        from .const import EVENT_CLICK

        class Button(EventEntity):
            _attr_event_types = [EVENT_CLICK]
    """,
}


@pytest.fixture
def source(tmp_path: Path) -> Path:
    root = tmp_path / "homeassistant"
    for name, text in SOURCE.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text), encoding="utf-8")
    return root


def _by_constant(catalogue: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {e.get("constant") or e["event_type"]: e for e in catalogue["events"]}


def test_constants_are_judged_by_how_they_are_used(source: Path):
    found = _by_constant(scan.catalogue(source, "2026.11.0", {}))

    assert found["EVENT_STATE_CHANGED"]["kind"] == "event"
    assert found["EVENT_STATE_CHANGED"]["evidence"] == ["typed EventType"]
    # fired from another module, through `from .const import`
    assert found["EVENT_CALL_SERVICE"]["kind"] == "event"
    assert found["EVENT_CALL_SERVICE"]["integration"] == "Home Assistant core"
    # through `from . import const`, and an f-string of DOMAIN
    turned = found["EVENT_WIDGET_TURNED"]
    assert (turned["kind"], turned["event_type"], turned["certain"]) == (
        "event",
        "widget_turned",
        True,
    )
    assert (turned["domain"], turned["integration"]) == ("widget", "Widget")
    assert turned["url"].startswith(
        "https://github.com/home-assistant/core/blob/2026.11.0/homeassistant/components/widget/const.py#L"
    )
    assert found["EVENT_DATA_ANGLE"]["kind"] == "data_key"
    assert found["EVENT_CLICK"]["kind"] == "entity_event_type"
    assert found["EVENT_WAIT"]["kind"] == "not_event"


def test_heuristics_are_marked_as_probable(source: Path):
    found = _by_constant(scan.catalogue(source, "2026.11.0", {}))

    timer = found["EVENT_TIMER_DONE"]
    assert (timer["kind"], timer["certain"]) == ("event", False)
    assert timer["evidence"] == ["named where a variable is fired"]
    ping = found["EVENT_WIDGET_PING"]
    assert (ping["kind"], ping["certain"]) == ("event", False)
    assert ping["evidence"] == ["value starts with its domain"]
    assert (found["EVENT_UID"]["kind"], found["EVENT_UID"]["certain"]) == (
        "not_event",
        False,
    )
    assert found["EVENT_RIPPLE"]["kind"] == "unknown"


def test_an_event_fired_as_a_string_is_listed(source: Path):
    found = _by_constant(scan.catalogue(source, "2026.11.0", {}))

    literal = found["widget_literal"]
    assert (literal["kind"], literal["certain"]) == ("event", True)
    assert "constant" not in literal


def test_overrides_correct_and_add(source: Path, capsys):
    overrides = {
        "fix": [
            {
                "module": "homeassistant.components.widget.const",
                "constant": "EVENT_RIPPLE",
                "kind": "data_key",
                "note": "Checked by hand",
            },
            {
                "module": "homeassistant.components.gone",
                "constant": "EVENT_GONE",
                "kind": "event",
            },
        ],
        "add": [
            {"event_type": "app_event", "domain": "widget", "note": "Fired by an app"}
        ],
    }
    found = _by_constant(scan.catalogue(source, "2026.11.0", overrides))

    ripple = found["EVENT_RIPPLE"]
    assert (ripple["kind"], ripple["certain"], ripple["note"]) == (
        "data_key",
        True,
        "Checked by hand",
    )
    assert found["app_event"]["evidence"] == ["overrides.toml"]
    assert (
        "nothing matches homeassistant.components.gone EVENT_GONE"
        in capsys.readouterr().err
    )


def test_main_writes_the_catalogue(source: Path, tmp_path: Path):
    out = tmp_path / "events.json"
    overrides = tmp_path / "overrides.toml"
    overrides.write_text('[[add]]\nevent_type = "app_event"\n', encoding="utf-8")

    assert (
        scan.main([
            str(source),
            "--version",
            "2026.11.0",
            "--overrides",
            str(overrides),
            "--out",
            str(out),
        ])
        == 0
    )

    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["home_assistant"] == "2026.11.0"
    assert written["counts"]["event"] == sum(
        e["kind"] == "event" for e in written["events"]
    )
    kinds = [e["kind"] for e in written["events"]]
    assert kinds == sorted(kinds, key=scan.KINDS.index)
