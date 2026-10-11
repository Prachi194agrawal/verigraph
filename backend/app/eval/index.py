"""BuildIndex: an in-memory mirror of one build's symbols and precise facts, used by every scorer.

Loading the graph once per build (instead of one Cypher query per claim) keeps scoring fast, and lets the
scorers run on an in-memory CodeGraph in unit tests with no database at all.
"""

from __future__ import annotations

import json
import threading
from collections import defaultdict
from dataclasses import dataclass

from app.config import PRECISE_RESOLVERS
from app.graph.languages import detect_language
from app.graph.model import CodeGraph, Param
from app.graph.modules import resolve_relative
from app.graph.store import GraphStore
from app.graph.symbols import BUILTIN_NAMES, ClassHierarchy, Target


@dataclass
class FnInfo:
    qualified_name: str
    name: str
    module: str
    file_path: str
    class_qn: str | None
    params: list[Param]
    kind: str
    is_test: bool
    is_nested: bool
    unresolved_calls: list[str]
    returns: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    pytest_id: str | None = None
    language: str = "python"


class BuildIndex(ClassHierarchy):
    def __init__(self, functions: list[dict], classes: list[dict], files: list[dict], calls: list[dict] = (),
                 inherits: list[dict] = (), modifies: list[dict] = ()) -> None:
        super().__init__()
        self.functions: dict[str, FnInfo] = {}
        for r in functions:
            params = [Param(**p) for p in json.loads(r.get("params_json") or "[]")]
            fn = FnInfo(r["qualified_name"], r["name"], r["module"], r["file_path"], r.get("class_qn"), params,
                        r.get("kind") or "function", bool(r.get("is_test")), bool(r.get("is_nested")),
                        list(r.get("unresolved_calls") or []), r.get("returns"), r.get("start_line"),
                        r.get("end_line"), r.get("pytest_id"), r.get("language") or _language(r["file_path"]))
            self.functions[fn.qualified_name] = fn
            if fn.class_qn:
                self._methods[fn.class_qn].setdefault(fn.name, fn.qualified_name)
        self.classes: dict[str, dict] = {r["qualified_name"]: r for r in classes}
        for r in classes:
            self._bases[r["qualified_name"]] = [b for b in r.get("bases") or [] if b]
            self._external_bases[r["qualified_name"]] = list(r.get("external_bases") or [])
            self._attributes[r["qualified_name"]] = set(r.get("attributes") or [])
        self.files: dict[str, str] = {r["path"]: r["module"] for r in files}
        self.file_languages = {r["path"]: r.get("language") or _language(r["path"]) for r in files}
        self.analysis_levels = {r["path"]: r.get("analysis_level") or
                               ("semantic" if self.file_languages[r["path"]] == "python" else "structural")
                               for r in files}
        self.test_files = {r["path"] for r in files if r.get("is_test")}
        self.modules: dict[str, str] = {r["module"]: r["path"] for r in files}
        self.namespaces: dict[str, dict[str, list[str]]] = {
            r["module"]: json.loads(r.get("namespace_json") or "{}") for r in files}
        self._packages = {m.rsplit(".", i)[0] for m in self.modules for i in range(1, m.count(".") + 1)}

        self.calls: dict[tuple[str, str], str] = {(r["caller"], r["callee"]): r["resolved_by"] for r in calls}
        self.call_lines: dict[tuple[str, str], int] = {(r["caller"], r["callee"]): r.get("line") for r in calls}
        self.precise_out: dict[str, set[str]] = defaultdict(set)
        self.precise_in: dict[str, set[str]] = defaultdict(set)
        self.all_out: dict[str, set[str]] = defaultdict(set)
        self.all_in: dict[str, set[str]] = defaultdict(set)
        for (a, b), by in self.calls.items():
            self.all_out[a].add(b)
            self.all_in[b].add(a)
            if by in PRECISE_RESOLVERS:
                self.precise_out[a].add(b)
                self.precise_in[b].add(a)
        self.inherits: dict[tuple[str, str], str] = {(r["child"], r["parent"]): r["resolved_by"] for r in inherits}
        self.parents: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for (c, p), by in self.inherits.items():
            self.parents[c].append((p, by))
        self.modified_by: dict[str, set[str]] = defaultdict(set)
        for r in modifies:
            for ident in (r.get("name"), r.get("email"), r.get("login")):
                if ident:
                    self.modified_by[r["function"]].add(ident.lower())

        self._suffix: dict[str, set[str]] = defaultdict(set)
        for qn in [*self.functions, *self.classes]:
            parts = qn.split(".")
            for i in range(len(parts)):
                self._suffix[".".join(parts[i:])].add(qn)
        self._path_suffix: dict[str, set[str]] = defaultdict(set)
        for path in self.files:
            parts = path.split("/")
            for i in range(len(parts)):
                self._path_suffix["/".join(parts[i:])].add(path)
        self._children: dict[str, set[str]] = defaultdict(set)
        for child, parents in self._bases.items():
            for parent in parents:
                self._children[parent].add(child)

    @classmethod
    def from_rows(cls, rows: dict[str, list[dict]]) -> BuildIndex:
        return cls(**rows)

    # -- lookups -----------------------------------------------------------------------------------------

    def symbols_ending_with(self, dotted: str) -> set[str]:
        return self._suffix.get(dotted, set())

    def paths_ending_with(self, path: str) -> set[str]:
        return self._path_suffix.get(path, set())

    def is_module(self, name: str) -> bool:
        return name in self.modules or name in self._packages

    def is_entity(self, qn: str) -> bool:
        return qn in self.functions or qn in self.classes

    def module_for_path(self, path: str) -> str | None:
        matches = self.paths_ending_with(path.lstrip("/"))
        return self.files[next(iter(matches))] if len(matches) == 1 else None

    def file_of(self, qn: str) -> str | None:
        if qn in self.functions:
            return self.functions[qn].file_path
        if qn in self.classes:
            return self.classes[qn]["file_path"]
        return None

    def subclasses(self, class_qn: str) -> set[str]:
        """Every repository class that inherits from `class_qn`, directly or indirectly."""
        out: set[str] = set()
        stack = [class_qn]
        while stack:
            for child in self._children.get(stack.pop(), ()):
                if child not in out:
                    out.add(child)
                    stack.append(child)
        return out

    def defined_in_subclass(self, class_qn: str, attr: str) -> bool:
        """`self.attr` inside a base class or mixin is valid when a subclass provides it."""
        return any(self._methods.get(c, {}).get(attr) or attr in self._attributes.get(c, ())
                   or f"{c}.{attr}" in self.classes for c in self.subclasses(class_qn))

    def is_package_module(self, module: str) -> bool:
        return self.modules.get(module, "").endswith("__init__.py")

    def resolve_relative(self, module: str, level: int, dotted: str | None) -> str | None:
        return resolve_relative(module, self.is_package_module(module), level, dotted or "")

    # -- the same name semantics as graph.symbols.SymbolTable, but from stored namespaces --------------

    def resolve_name(self, module: str, name: str) -> Target | None:
        entry = self.namespaces.get(module, {}).get(name)
        if entry:
            return Target(entry[0], entry[1])
        if name in BUILTIN_NAMES:
            return Target("external", "builtins")
        return None

    def member(self, target: Target, attr: str) -> Target | None:
        if target.kind == "module":
            sub = f"{target.name}.{attr}"
            if self.is_module(sub):
                return Target("module", sub)
            return self.resolve_name(target.name, attr) if target.name in self.modules else None
        if target.kind in ("class", "instance"):
            method = self.lookup_method(target.name, attr)
            if method:
                return Target("function", method)
            if f"{target.name}.{attr}" in self.classes:
                return Target("class", f"{target.name}.{attr}")
            if self.has_attribute(target.name, attr):
                return Target("value", f"{target.name}.{attr}")
            if self.has_external_base(target.name):
                return Target("external", "inherited")
            return None
        if target.kind == "external":
            return target
        return None

    # -- graph facts -------------------------------------------------------------------------------------

    def reaches(self, src: str, dst: str, max_hops: int, precise_only: bool = True) -> int | None:
        """Hop count of the shortest CALLS path src -> dst (<= max_hops), or None."""
        frontier, seen = {src}, {src}
        for hop in range(1, max_hops + 1):
            nxt: set[str] = set()
            adjacency = self.precise_out if precise_only else self.all_out
            for node in frontier:
                nxt |= adjacency.get(node, set())
            if dst in nxt:
                return hop
            frontier = nxt - seen
            seen |= nxt
        return None

    def inherits_from(self, child: str, parent: str) -> str | None:
        """'precise' if a path of precise INHERITS edges exists, 'name' if only via name edges, else None."""
        best = None
        stack = [(child, True)]
        visited = set()
        while stack:
            node, precise = stack.pop()
            if (node, precise) in visited:
                continue
            visited.add((node, precise))
            for p, by in self.parents.get(node, []):
                path_precise = precise and by in PRECISE_RESOLVERS
                if p == parent:
                    if path_precise:
                        return "precise"
                    best = "name"
                stack.append((p, path_precise))
        return best


_cache: dict[int, BuildIndex] = {}
_lock = threading.Lock()


def load_build_index(store: GraphStore, build_id: int, refresh: bool = False) -> BuildIndex:
    with _lock:
        if refresh or build_id not in _cache:
            _cache[build_id] = BuildIndex.from_rows(store.load_index_rows(build_id))
        return _cache[build_id]


def evict_build_index(build_id: int) -> None:
    with _lock:
        _cache.pop(build_id, None)


def index_rows_from_graph(g: CodeGraph) -> dict[str, list[dict]]:
    """The rows `GraphStore.load_index_rows` would return for this graph (used by tests)."""
    functions = [{
        "qualified_name": f.qualified_name, "name": f.name, "module": f.module, "file_path": f.file_path,
        "class_qn": f.class_qn, "params_json": json.dumps([p.to_dict() for p in f.params]), "kind": f.kind,
        "is_test": f.is_test, "is_nested": f.is_nested, "start_line": f.start_line, "end_line": f.end_line,
        "unresolved_calls": sorted(set(g.unresolved.get(f.qualified_name, []))), "returns": f.returns,
        "pytest_id": f.pytest_id,
        "language": f.language,
    } for f in g.functions.values()]
    parents: dict[str, list[str]] = defaultdict(list)
    for child, parent in g.inherits:
        parents[child].append(parent)
    classes = [{
        "qualified_name": c.qualified_name, "name": c.name, "module": c.module, "file_path": c.file_path,
        "external_bases": list(g.external_bases.get(c.qualified_name, [])), "attributes": sorted(c.attributes),
        "bases": parents.get(c.qualified_name, []),
        "language": c.language,
    } for c in g.classes.values()]
    files = [{"path": f.path, "module": f.module, "is_test": f.is_test, "namespace_json": json.dumps(f.namespace),
              "language": f.language, "analysis_level": f.analysis_level}
             for f in g.files.values()]
    calls = [{"caller": e.caller, "callee": e.callee, "resolved_by": e.resolved_by, "line": min(e.lines)}
             for e in g.calls.values()]
    inherits = [{"child": c, "parent": p, "resolved_by": by} for (c, p), by in g.inherits.items()]
    modifies = []
    for (sha, fn) in g.modifies:
        c = g.commits[sha]
        modifies.append({"function": fn, "name": c.author_name, "email": c.author_email,
                         "login": g.author_logins.get(c.author_email)})
    return {"functions": functions, "classes": classes, "files": files, "calls": calls, "inherits": inherits,
            "modifies": modifies}


def _language(path: str) -> str:
    spec = detect_language(path)
    return spec.name if spec else "unknown"


def codegen_language(index: BuildIndex, meta: dict) -> str:
    """Prefer stored symbols and source paths to optional imported task metadata."""
    original = index.functions.get(meta.get("blank", ""))
    if original is not None:
        return original.language
    if meta.get("file"):
        return _language(meta["file"])
    return meta.get("language") or "python"  # legacy Python-only tasks stored module/blank without a file
