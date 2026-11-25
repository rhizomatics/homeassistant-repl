"""Group top-level pages and sections under one tab, without moving their
files - so their URLs, linked from the changelog, READMEs and elsewhere,
stay as they are.

The nav is otherwise built from the folder layout, as properdocs does with no
`nav:` in properdocs.yml, so a new page still appears without listing it
anywhere. A group takes the place of the first of its members.
"""

from __future__ import annotations

from typing import Any

from properdocs.structure.nav import (
    Section,
    _add_parent_links,
    _add_previous_and_next_links,
    _get_by_type,
)
from properdocs.structure.pages import Page

# Tab title -> its members, in order: a page by its file, a section by its folder
GROUPS = {
    "Usage": ["modes/", "obj_tree.md", "privacy.md", "sql.md"],
}


def _is(item: Any, member: str) -> bool:
    if member.endswith("/"):
        return bool(item.is_section) and any(
            page.file.src_uri.startswith(member)
            for page in _get_by_type(item.children, Page)
        )
    return bool(item.is_page) and item.file.src_uri == member


def on_nav(nav, config, files):
    for title, members in GROUPS.items():
        found = {}
        for member in members:
            for item in nav.items:
                if _is(item, member):
                    found[member] = item
        if not found:
            continue
        group = Section(title, [found[m] for m in members if m in found])
        group.parent = None
        at = min(nav.items.index(item) for item in found.values())
        nav.items = [item for item in nav.items if item not in found.values()]
        nav.items.insert(at, group)
    # What properdocs works out once, when it builds the nav: each item's
    # parent, and the previous and next page links, which follow nav order
    _add_parent_links(nav.items)
    nav.pages = _get_by_type(nav.items, Page)
    _add_previous_and_next_links(nav.pages)
    return nav
