"""Stop mkdocs-pagetree-plugin failing on a section nested inside a section."""

from properdocs.structure.nav import Section

# `{{ pagetree(siblings) }}` reduces each sibling section to its index page, looking for one
# with `child.is_index` - which a page has, but a section inside that section does not
Section.is_index = False  # type: ignore[attr-defined] # ty: ignore[missing-attribute]
