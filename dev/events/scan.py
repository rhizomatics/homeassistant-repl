"""Build the event catalogue: the event types Home Assistant core declares
and fires, found in its source without importing or running any of it.

    python -I dev/events/scan.py SOURCE --version 2026.11.0

SOURCE is the `homeassistant` package directory, from a source checkout or a
release tarball. Writes dev/events/events.json, after applying
dev/events/overrides.toml. Standard library only, so the weekly job needs
nothing installed.

Home Assistant has no registry of event types: an event type is any string
given to `hass.bus.async_fire`. So this starts from what developers declare,
module level `EVENT_*` constants, and decides what each one is from how the
code uses it rather than from its name, since the prefix is also used for
event data keys, event entity types and settings. The event types fired as
string literals, with no constant, are added too.

What each verdict rests on is kept as `evidence`, so a wrong one can be
traced and corrected in overrides.toml.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
import tomllib
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

HERE = Path(__file__).parent
GITHUB = "https://github.com/home-assistant/core/blob/{version}/{path}#L{line}"

FIRE = frozenset({"async_fire", "fire", "async_fire_internal"})
LISTEN = frozenset({"async_listen", "async_listen_once", "listen", "listen_once"})

# What a constant is, most certain first. "probable" verdicts rest on a
# heuristic rather than on how the constant is used.
EVENT = "event"
ENTITY_EVENT_TYPE = "entity_event_type"
DATA_KEY = "data_key"
NOT_EVENT = "not_event"
UNKNOWN = "unknown"
KINDS = (EVENT, ENTITY_EVENT_TYPE, DATA_KEY, NOT_EVENT, UNKNOWN)


@dataclass
class Entry:
    event_type: str | None
    kind: str
    certain: bool
    evidence: list[str]
    domain: str | None
    integration: str | None
    module: str
    line: int
    constant: str | None = None
    note: str | None = None
    url: str | None = None


@dataclass
class _Module:
    name: str
    path: Path
    tree: ast.Module
    is_package: bool
    # name -> the node assigned to it, for every module level assignment
    assigned: dict[str, tuple[ast.expr, int]] = field(default_factory=dict)
    # local name -> (module, name) it was imported as
    imported: dict[str, tuple[str, str]] = field(default_factory=dict)
    # local name -> module, for `import x.y as z` and `from . import const`
    modules: dict[str, str] = field(default_factory=dict)


class Scan:
    def __init__(self, source: Path) -> None:
        self.source = source
        self.modules: dict[str, _Module] = {}
        for path in sorted(source.rglob("*.py")):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError, UnicodeDecodeError, ValueError:
                continue
            parts = path.relative_to(source.parent).with_suffix("").parts
            is_package = parts[-1] == "__init__"
            name = ".".join(parts[:-1] if is_package else parts)
            self.modules[name] = _Module(name, path, tree, is_package)
        for module in self.modules.values():
            self._index(module)
        self.names = {m.name: _integration_names(m) for m in self.modules.values()}
        # (module, constant) -> roles it's used in
        self.roles: dict[tuple[str, str], set[str]] = defaultdict(set)
        self.fired_literals: dict[str, tuple[str, int]] = {}
        for module in self.modules.values():
            self._uses(module)

    # -- the module level symbol table ------------------------------------

    def _index(self, module: _Module) -> None:
        package = module.name if module.is_package else module.name.rpartition(".")[0]
        for node in module.tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        module.assigned[target.id] = (node.value, node.lineno)
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                if isinstance(node.target, ast.Name):
                    module.assigned[node.target.id] = (node.value, node.lineno)
                    if "EventType" in ast.unparse(node.annotation):
                        module.assigned[f"\0typed:{node.target.id}"] = (node.value, 0)
            elif isinstance(node, ast.ImportFrom):
                base = _relative(package, node.module, node.level)
                for alias in node.names:
                    local = alias.asname or alias.name
                    if f"{base}.{alias.name}" in self.modules:
                        module.modules[local] = f"{base}.{alias.name}"
                    else:
                        module.imported[local] = (base, alias.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.asname:
                        module.modules[alias.asname] = alias.name

    def resolve(self, module: str, name: str, depth: int = 0) -> tuple[str, str] | None:
        """Where `name`, as used in `module`, is assigned."""
        found = self.modules.get(module)
        if found is None or depth > 10:
            return None
        if name in found.assigned:
            return module, name
        if name in found.imported:
            return self.resolve(*found.imported[name], depth + 1)
        return None

    def resolve_node(self, module: str, node: ast.expr) -> tuple[str, str] | None:
        """The constant a Name, or a `const.NAME` attribute, refers to."""
        if isinstance(node, ast.Name):
            return self.resolve(module, node.id)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            target = self.modules[module].modules.get(node.value.id)
            if target is not None:
                return self.resolve(target, node.attr)
        return None

    def string(self, module: str, node: ast.expr, depth: int = 0) -> str | None:
        """A node's value, if it's a string known without running anything:
        a literal, a constant naming one, or an f-string of those."""
        if depth > 10:
            return None
        if isinstance(node, ast.Constant):
            return node.value if isinstance(node.value, str) else None
        if isinstance(node, ast.Call) and _called(node) == "EventType" and node.args:
            return self.string(module, node.args[0], depth + 1)
        if isinstance(node, ast.JoinedStr):
            parts = []
            for value in node.values:
                if isinstance(value, ast.FormattedValue):
                    if value.format_spec is not None or value.conversion != -1:
                        return None
                    value = value.value
                part = self.string(module, value, depth + 1)
                if part is None:
                    return None
                parts.append(part)
            return "".join(parts)
        if (where := self.resolve_node(module, node)) is not None:
            target, name = where
            return self.string(
                target, self.modules[target].assigned[name][0], depth + 1
            )
        return None

    # -- how constants are used -------------------------------------------

    def _mark(self, module: str, node: ast.AST, role: str) -> None:
        for sub in ast.walk(node):
            if isinstance(sub, (ast.Name, ast.Attribute)):
                where = self.resolve_node(module, sub)
                if where is not None and where[1].startswith("EVENT_"):
                    self.roles[where].add(role)

    def _uses(self, module: _Module) -> None:
        name = module.name
        for node in ast.walk(module.tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                method = node.func.attr
                first = node.args[0] if node.args else None
                if method in FIRE and first is not None:
                    self._mark(name, first, "fired")
                    literal = self.string(name, first)
                    if literal is not None and isinstance(
                        first, (ast.Constant, ast.JoinedStr)
                    ):
                        self.fired_literals.setdefault(literal, (name, node.lineno))
                    if len(node.args) > 1 and isinstance(node.args[1], ast.Dict):
                        for key in node.args[1].keys:
                            if key is not None:
                                self._mark(name, key, "data key")
                elif method in LISTEN and first is not None:
                    self._mark(name, first, "listened for")
                elif method == "_trigger_event" and first is not None:
                    self._mark(name, first, "event entity type")
            elif isinstance(node, ast.keyword):
                if node.arg == "event_types":
                    self._mark(name, node.value, "event entity type")
                elif node.arg == "event_type":
                    self._mark(name, node.value, "event_type= argument")
            elif isinstance(node, ast.Assign):
                if any(_target_name(t) == "_attr_event_types" for t in node.targets):
                    self._mark(name, node.value, "event entity type")
            elif isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values, strict=True):
                    if key is not None and _is_event_type_key(key):
                        self._mark(name, value, "event_type in a trigger")
        # A scope that fires a variable: any EVENT_ constant it names is
        # probably what's fired - timer's `self.hass.bus.async_fire(event, ...)`
        scopes = [
            n
            for n in ast.walk(module.tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        ]
        for scope in [*scopes, module.tree]:
            if any(_fires_variable(n) for n in ast.walk(scope)):
                self._mark(name, scope, "named where a variable is fired")

    # -- the catalogue ----------------------------------------------------

    def entries(self, version: str) -> list[Entry]:
        found: list[Entry] = []
        declared: set[str] = set()
        for module in self.modules.values():
            for constant, (node, line) in module.assigned.items():
                if not constant.startswith("EVENT_"):
                    continue
                roles = self.roles.get((module.name, constant), set())
                if f"\0typed:{constant}" in module.assigned:
                    roles = roles | {"typed EventType"}
                if isinstance(node, ast.Call) and _called(node) == "EventType":
                    roles = roles | {"typed EventType"}
                value = self.string(module.name, node)
                kind, certain, evidence = _verdict(
                    value, roles, self.names[module.name][0]
                )
                found.append(
                    self._entry(
                        module, value, kind, certain, evidence, line, version, constant
                    )
                )
                if kind == EVENT and value is not None:
                    declared.add(value)
        for value, (module_name, line) in sorted(self.fired_literals.items()):
            if value not in declared:
                found.append(
                    self._entry(
                        self.modules[module_name],
                        value,
                        EVENT,
                        True,
                        ["fired as a string, with no constant"],
                        line,
                        version,
                        None,
                    )
                )
        return found

    def _entry(
        self,
        module: _Module,
        value: str | None,
        kind: str,
        certain: bool,
        evidence: list[str],
        line: int,
        version: str,
        constant: str | None,
    ) -> Entry:
        domain, integration = self.names[module.name]
        path = module.path.relative_to(self.source.parent).as_posix()
        return Entry(
            event_type=value,
            kind=kind,
            certain=certain,
            evidence=evidence,
            domain=domain,
            integration=integration,
            module=module.name,
            line=line,
            constant=constant,
            url=GITHUB.format(version=version, path=path, line=line),
        )


def _verdict(
    value: str | None, roles: set[str], domain: str | None
) -> tuple[str, bool, list[str]]:
    if value is None:
        return NOT_EVENT, True, ["not a string"]
    used_as_event = sorted(
        roles & {"fired", "listened for", "typed EventType", "event_type in a trigger"}
    )
    if used_as_event:
        return EVENT, True, used_as_event
    if "event entity type" in roles:
        return ENTITY_EVENT_TYPE, True, ["event entity type"]
    if "data key" in roles:
        return DATA_KEY, True, ["data key"]
    if any(c.isspace() for c in value) or "/" in value or value.startswith("{"):
        return NOT_EVENT, False, ["not shaped like an event type"]
    if "_" not in value and "." not in value:
        return NOT_EVENT, False, ["a single word"]
    if "named where a variable is fired" in roles:
        return EVENT, False, ["named where a variable is fired"]
    if "event_type= argument" in roles:
        return EVENT, False, ["event_type= argument"]
    if domain and value.startswith((f"{domain}_", f"{domain}.")):
        return EVENT, False, ["value starts with its domain"]
    return UNKNOWN, False, []


def apply_overrides(entries: list[Entry], overrides: dict[str, Any]) -> list[str]:
    """Correct entries from overrides.toml, and add the events it lists.
    Returns the overrides that matched nothing, so a stale one is noticed."""
    unused = []
    for fix in overrides.get("fix", []):
        matched = [
            e
            for e in entries
            if e.module == fix["module"]
            and (
                e.constant == fix.get("constant")
                or e.event_type == fix.get("event_type")
            )
        ]
        if not matched:
            unused.append(
                f"{fix['module']} {fix.get('constant') or fix.get('event_type')}"
            )
        for entry in matched:
            for key in ("kind", "event_type", "note"):
                if key in fix:
                    setattr(entry, key, fix[key])
            if "kind" in fix:
                entry.certain = True
                entry.evidence = [*entry.evidence, "overrides.toml"]
    for add in overrides.get("add", []):
        entries.append(
            Entry(
                event_type=add["event_type"],
                kind=add.get("kind", EVENT),
                certain=True,
                evidence=["overrides.toml"],
                domain=add.get("domain"),
                integration=add.get("integration"),
                module=add.get("module", ""),
                line=0,
                note=add.get("note"),
            )
        )
    return unused


def catalogue(source: Path, version: str, overrides: dict[str, Any]) -> dict[str, Any]:
    entries = Scan(source).entries(version)
    unused = apply_overrides(entries, overrides)
    for stale in unused:
        print(f"overrides.toml: nothing matches {stale}", file=sys.stderr)
    entries.sort(
        key=lambda e: (
            KINDS.index(e.kind),
            (e.event_type or "").lower(),
            e.module,
            e.constant or "",
        )
    )
    return {
        "home_assistant": version,
        "counts": {kind: sum(e.kind == kind for e in entries) for kind in KINDS},
        "events": [
            {k: v for k, v in asdict(e).items() if v is not None} for e in entries
        ],
    }


def _integration_names(module: _Module) -> tuple[str | None, str | None]:
    """The integration a module belongs to, by domain and display name."""
    parts = module.name.split(".")
    if len(parts) < 3 or parts[1] != "components":
        return None, "Home Assistant core"
    manifest = module.path.parent
    while manifest.name != parts[2]:
        manifest = manifest.parent
    try:
        name = json.loads((manifest / "manifest.json").read_text(encoding="utf-8"))[
            "name"
        ]
    except OSError, ValueError, KeyError:
        name = parts[2]
    return parts[2], name


def _relative(package: str, module: str | None, level: int) -> str:
    if level == 0:
        return module or ""
    base = package
    for _ in range(level - 1):
        base = base.rpartition(".")[0]
    return f"{base}.{module}" if module else base


def _called(node: ast.Call) -> str:
    func = node.func
    return func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")


def _target_name(node: ast.expr) -> str:
    return getattr(node, "attr", getattr(node, "id", ""))


def _is_event_type_key(node: ast.expr) -> bool:
    if isinstance(node, ast.Constant):
        return node.value == "event_type"
    return _target_name(node) == "CONF_EVENT_TYPE"


def _fires_variable(node: ast.AST) -> bool:
    if not (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in FIRE
        and node.args
    ):
        return False
    first = node.args[0]
    return not isinstance(first, (ast.Constant, ast.JoinedStr)) and not (
        isinstance(first, (ast.Name, ast.Attribute))
        and _target_name(first).startswith("EVENT_")
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("source", type=Path, help="the homeassistant package directory")
    parser.add_argument(
        "--version", required=True, help="the Home Assistant release, e.g. 2026.11.0"
    )
    parser.add_argument("--overrides", type=Path, default=HERE / "overrides.toml")
    parser.add_argument("--out", type=Path, default=HERE / "events.json")
    args = parser.parse_args(argv)
    overrides = (
        tomllib.loads(args.overrides.read_text(encoding="utf-8"))
        if args.overrides.exists()
        else {}
    )
    result = catalogue(args.source, args.version, overrides)
    args.out.write_text(
        json.dumps(result, indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"{args.out}: " + ", ".join(f"{n} {k}" for k, n in result["counts"].items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
