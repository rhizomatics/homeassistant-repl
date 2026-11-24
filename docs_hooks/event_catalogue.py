"""The event catalogue page, its tables and its CSV and JSON downloads, all
from dev/events/events.json, which dev/events/scan.py writes.

A page with `unlisted: true` in its front matter is kept out of the
sitemap (overrides/sitemap.xml) and away from search engines
(overrides/main.html), and with the theme's own `search: exclude: true` out
of the site's search, so it can be reviewed at its URL before anything
points people to it. `not_in_nav` in properdocs.yml keeps it out of the nav.
"""

from __future__ import annotations

import csv
import io
import json
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
SECTIONS = [
    ("event", "Events", True),
    ("entity_event_type", "Event Entity Types", True),
    ("unknown", "Not Yet Decided", False),
    ("data_key", "Event Data Keys", False),
    ("not_event", "Not Events", False),
]
ABOUT = {
    "event": "Event types fired on Home Assistant's event bus.",
    "entity_event_type": (
        "The `event_type` of an event entity's state, not an event on the bus of its own: "
        "an event entity fires `state_changed` on itself, with this as its `event_type` attribute."
    ),
    "unknown": "Constants named `EVENT_*` whose use the scan couldn't decide. Each wants a look at the code, and an entry in `overrides.toml`.",
    "data_key": "Keys in the data of an event, named `EVENT_*`.",
    "not_event": "Constants named `EVENT_*` that aren't event types: timeouts, schemas, field names and the like.",
}


def _load() -> dict[str, Any]:
    return json.loads(CATALOGUE.read_text(encoding="utf-8"))


def _csv(catalogue: dict[str, Any]) -> str:
    out = io.StringIO()
    writer = csv.DictWriter(out, COLUMNS, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for entry in catalogue["events"]:
        writer.writerow({**entry, "evidence": "; ".join(entry.get("evidence", []))})
    return out.getvalue()


def _cell(text: Any) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ") if text else ""


def _code(text: Any) -> str:
    return f"`{text}`" if text else ""


def _table(entries: list[dict[str, Any]]) -> str:
    rows = [
        "| Event type | Integration | Constant | How it was found | Source |",
        "| --- | --- | --- | --- | --- |",
    ]
    for entry in entries:
        found = ", ".join(entry.get("evidence", []))
        if not entry["certain"]:
            found = f"*probably*: {found}" if found else ""
        if entry.get("note"):
            found = f"{found}. {entry['note']}" if found else entry["note"]
        source = (
            f"[{entry['module'].removeprefix('homeassistant.')}]({entry['url']})"
            if entry.get("url")
            else ""
        )
        rows.append(
            f"| {_code(_cell(entry.get('event_type')))} | {_cell(entry.get('integration'))} "
            f"| {_code(entry.get('constant'))} | {_cell(found)} | {source} |"
        )
    return "\n".join(rows)


def render(catalogue: dict[str, Any]) -> str:
    counts = catalogue["counts"]
    parts = [
        (
            f"Scanned from Home Assistant **{catalogue['home_assistant']}**: "
            f"{counts['event']} events, {counts['entity_event_type']} event entity types, "
            f"and {counts['unknown']} constants not yet decided."
        ),
        f"Download: [CSV, for a spreadsheet]({DOWNLOADS}.csv) · [JSON]({DOWNLOADS}.json)",
    ]
    for kind, title, open_ in SECTIONS:
        entries = [e for e in catalogue["events"] if e["kind"] == kind]
        if not entries:
            continue
        heading = f"{title} ({len(entries)})"
        body = f"{ABOUT[kind]}\n\n{_table(entries)}"
        if open_:
            parts.append(f"## {heading}\n\n{body}")
        else:
            # Collapsed: there for review, not for reading through.
            indented = "\n".join(
                f"    {line}" if line else "" for line in body.splitlines()
            )
            parts.append(f'??? note "{heading}"\n\n{indented}')
    return "\n\n".join(parts)


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
