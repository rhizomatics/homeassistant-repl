// Sorting and filtering for the event catalogue page, whose table
// docs_hooks/event_catalogue.py writes. Loaded on every page, but does nothing
// where there's no `.event-catalogue`. Each row carries its kind and whether
// that's certain as data attributes, so nothing is read back out of the cells
// but the text being searched or sorted.

(function () {
  "use strict";

  function setUp(root) {
    if (root.dataset.ready) {
      return;
    }
    root.dataset.ready = "true";
    const controls = root.querySelector(".ec-controls");
    const tbody = root.querySelector("tbody");
    const rows = Array.from(tbody.rows);
    const text = new Map(rows.map((row) => [row, row.textContent.toLowerCase()]));
    const search = controls.querySelector("input[type=search]");
    const kinds = Array.from(controls.querySelectorAll("input[data-kind]"));
    const certain = controls.querySelector("input[data-certain]");
    const count = controls.querySelector(".ec-count");

    function filter() {
      const words = search.value.toLowerCase().split(/\s+/).filter(Boolean);
      const shown = new Set(kinds.filter((box) => box.checked).map((box) => box.dataset.kind));
      let showing = 0;
      for (const row of rows) {
        const match =
          shown.has(row.dataset.kind) &&
          (!certain.checked || row.dataset.certain === "true") &&
          words.every((word) => text.get(row).includes(word));
        row.hidden = !match;
        showing += match ? 1 : 0;
      }
      count.textContent = `Showing ${showing} of ${rows.length}`;
    }

    const headings = Array.from(root.querySelectorAll("thead th"));
    headings.forEach((heading, column) => {
      heading.querySelector("button").addEventListener("click", () => {
        const ascending = heading.getAttribute("aria-sort") !== "ascending";
        headings.forEach((other) => other.removeAttribute("aria-sort"));
        heading.setAttribute("aria-sort", ascending ? "ascending" : "descending");
        const key = (row) => row.cells[column].textContent.trim().toLowerCase();
        rows.sort((a, b) => {
          const [x, y] = [key(a), key(b)];
          // Empty cells last, whichever way round
          if (!x || !y) {
            return !x - !y;
          }
          return (ascending ? 1 : -1) * x.localeCompare(y);
        });
        tbody.append(...rows);
      });
    });

    for (const input of [search, certain, ...kinds]) {
      input.addEventListener("input", filter);
    }
    controls.hidden = false;
    filter();
  }

  function setUpAll() {
    document.querySelectorAll(".event-catalogue").forEach(setUp);
  }

  // The theme's instant navigation swaps pages in without reloading, and
  // tells subscribers to document$ each time it does.
  if (typeof document$ !== "undefined") {
    document$.subscribe(setUpAll);
  } else {
    document.addEventListener("DOMContentLoaded", setUpAll);
  }
})();
