"""Clean up page HTML before mkdocs-llmstxt converts it to Markdown."""

import re
from typing import Any

# pagetree macros are only expanded in the final HTML, after llmstxt has read the page
PAGETREE_MACRO = re.compile(r"^\s*\{\{\s*pagetree\(.*\)\s*\}\}\s*$")


# The event catalogue's rows an agent wants: the events themselves, not the constants
# that turned out not to be events - those stay on the page, and in events.json
CATALOGUE_KINDS = {"event", "entity_event_type"}


def preprocess(soup: Any, output: str) -> None:
    for p in soup.find_all("p"):
        if PAGETREE_MACRO.match(p.get_text()):
            p.decompose()
    for catalogue in soup.select(".event-catalogue"):
        # the filter and checkboxes need the page's script
        for controls in catalogue.select(".ec-controls"):
            controls.decompose()
        for row in catalogue.select("tbody tr"):
            if row.get("data-kind") not in CATALOGUE_KINDS:
                row.decompose()
        # Source, the last column: a long GitHub link on every row, which the
        # converter pads every other row out to - each entry's is in events.json
        for row in catalogue.select("tr"):
            row.find_all(["th", "td"])[-1].decompose()
        note = soup.new_tag("p")
        note.string = (
            "Standard events and entity events only, without links to their source. Every "
            "entry, with its source, including constants not yet decided and those that "
            "aren't events, is in events.json beside this page."
        )
        catalogue.insert_before(note)
