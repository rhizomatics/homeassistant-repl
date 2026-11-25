"""The event catalogue page, its tables and its CSV and JSON downloads, all
from dev/events/events.json, which dev/events/scan.py writes.

llms-full.txt gets a shorter form of the table - see
docs_hooks/llmstxt_preprocess.py.
"""

from __future__ import annotations

import csv
import io
import json
from html import escape
from pathlib import Path
from typing import Any

from properdocs.structure.files import File

MARKER = "<!-- event-catalogue -->"
CATALOGUE = Path(__file__).parent.parent / "dev" / "events" / "events.json"
DOWNLOADS = "events"
COLUMNS = [
    "event_type",
    "kind",
    "certain",
    "integration",
    "domain",
    "constant",
    "module",
    "line",
    "evidence",
    "note",
    "url",
]
# Each kind, as the page names it, whether it's shown before any filter is
# changed, and what it means.
KINDS = [
    ("event", "Standard", True, "An event type fired on Home Assistant's event bus."),
    (
        "entity_event_type",
        "Entity event",
        True,
        (
            "The `event_type` of an event entity's state, not an event on the bus of its own: "
            "an event entity fires `state_changed` on itself, with this as its `event_type` attribute."
        ),
    ),
    (
        "unknown",
        "Not yet decided",
        False,
        (
            "A constant named `EVENT_*` whose use the scan couldn't decide. Each wants a look at "
            "the code, and an entry in `overrides.toml`."
        ),
    ),
    ("data_key", "Data key", False, "A key in the data of an event, named `EVENT_*`."),
    (
        "not_event",
        "Not an event",
        False,
        "A constant named `EVENT_*` that isn't an event type: a timeout, schema or field name.",
    ),
]
HEADINGS = [
    "Event type",
    "Kind",
    "Integration",
    "Constant",
    "How it was found",
    "Source",
]


def _load() -> dict[str, Any]:
    return json.loads(CATALOGUE.read_text(encoding="utf-8"))


def _url(catalogue: dict[str, Any], entry: dict[str, Any]) -> str:
    """Where an entry's source is, from the catalogue's one link template -
    "" for an entry from overrides.toml, which has none."""
    if not entry.get("path"):
        return ""
    return str(catalogue["source_url"]).format(path=entry["path"], line=entry["line"])


def _csv(catalogue: dict[str, Any]) -> str:
    out = io.StringIO()
    writer = csv.DictWriter(out, COLUMNS, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for entry in catalogue["events"]:
        writer.writerow({
            **entry,
            "evidence": "; ".join(entry.get("evidence", [])),
            "url": _url(catalogue, entry),
        })
    return out.getvalue()


def _code(text: Any) -> str:
    """Breakable only after a `_` or `.`, so a long event type or constant
    wraps between its words, not inside one."""
    if not text:
        return ""
    breakable = escape(str(text)).replace("_", "_<wbr>").replace(".", ".<wbr>")
    return f"<code>{breakable}</code>"


def _row(entry: dict[str, Any], names: dict[str, str], url: str) -> str:
    found = escape(", ".join(entry.get("evidence", [])))
    if not entry["certain"]:
        found = f"<em>probably</em>: {found}" if found else ""
    if entry.get("note"):
        note = escape(entry["note"])
        found = f"{found}. {note}" if found else note
    source = (
        f'<a href="{escape(url)}">{escape(entry["module"].removeprefix("homeassistant."))}</a>'
        if url
        else ""
    )
    cells = [
        _code(entry.get("event_type")),
        names[entry["kind"]],
        escape(entry.get("integration") or ""),
        _code(entry.get("constant")),
        found,
        source,
    ]
    return (
        f'<tr data-kind="{entry["kind"]}" data-certain="{str(entry["certain"]).lower()}">'
        + "".join(f"<td>{cell}</td>" for cell in cells)
        + "</tr>"
    )


def _controls(counts: dict[str, int]) -> str:
    """Hidden until the page's script shows them, so a reader without
    JavaScript sees the whole table and no controls that do nothing."""
    boxes = "".join(
        f'<label><input type="checkbox" data-kind="{kind}"{" checked" if shown else ""}> '
        f"{name} ({counts.get(kind, 0)})</label>"
        for kind, name, shown, _ in KINDS
    )
    return (
        '<div class="ec-controls" hidden>'
        '<input type="search" placeholder="Filter by event type, integration, constant…" '
        'aria-label="Filter the catalogue">'
        f"<fieldset><legend>Show</legend>{boxes}"
        '<label><input type="checkbox" data-certain> Certain only</label></fieldset>'
        '<span class="ec-count" aria-live="polite"></span>'
        "</div>"
    )


def render(catalogue: dict[str, Any]) -> str:
    counts = catalogue["counts"]
    names = {kind: name for kind, name, _, _ in KINDS}
    heads = "".join(f'<th><button type="button">{h}</button></th>' for h in HEADINGS)
    rows = "".join(
        _row(entry, names, _url(catalogue, entry)) for entry in catalogue["events"]
    )
    kinds = "\n".join(f"- **{name}**: {about}" for _, name, _, about in KINDS)
    return "\n\n".join([
        (
            # The catalogue is only rewritten when its entries change, not
            # for each release (dev/events/scan.py --keep-if-same)
            f"As of Home Assistant **{catalogue['home_assistant']}**, the latest release "
            "that changed it: "
            f"{counts['event']} events, {counts['entity_event_type']} event entity types, "
            f"and {counts['unknown']} constants not yet decided."
        ),
        f"Download: [CSV, for a spreadsheet]({DOWNLOADS}.csv) · [JSON]({DOWNLOADS}.json)",
        "## Kinds",
        kinds,
        "## Catalogue",
        (
            "Click a column's heading to sort by it. Constants that aren't events are hidden "
            "to begin with: tick them under *Show* to review them."
        ),
        # One block of HTML, which the page's script sorts and filters -
        # docs/assets/javascripts/event_catalogue.js
        (
            f'<div class="event-catalogue">{_controls(counts)}'
            f"<table><thead><tr>{heads}</tr></thead><tbody>{rows}</tbody></table></div>"
        ),
    ])


def on_files(files, config):
    catalogue = _load()
    for name, content in (
        (f"events/{DOWNLOADS}.csv", _csv(catalogue)),
        (
            f"events/{DOWNLOADS}.json",
            json.dumps(catalogue, indent=1, ensure_ascii=False),
        ),
    ):
        files.append(File.generated(config, name, content=content))
    return files


def on_page_markdown(markdown, page, config, files):
    return markdown.replace(MARKER, render(_load())) if MARKER in markdown else markdown


def on_nav(nav, config, files):
    """Name the catalogue's tab after the page, not after its folder - which
    would make it "Events" - keeping its URL, /events/, as it is."""
    for item in nav.items:
        if item.is_section and any(
            getattr(child, "file", None) and child.file.src_uri == "events/index.md"
            for child in item.children
        ):
            item.title = "Event Catalogue"
    return nav
