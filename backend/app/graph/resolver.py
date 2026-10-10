"""Call resolution: decide which function each call site actually points to.

Resolvers are tried in order (default: scip, jedi, import, name) and the first one that reaches a
decision wins. A decision is either "resolved" (target functions in the repo), "external" (builtins,
stdlib, third-party) or nothing, in which case the next resolver is asked. Every CALLS edge records
`resolved_by`, and only scip/jedi/import edges may become gold answers.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from app.graph.model import CallSite, CodeGraph, FunctionDef
from app.graph.scip import decode_index
from app.graph.symbols import BUILTIN_NAMES, BUILTIN_TYPE_METHODS, SymbolTable, Target

log = logging.getLogger(__name__)


@dataclass
class Resolution:
    status: str  # resolved | external
    targets: list[str] = field(default_factory=list)
    by: str = ""
    ambiguity: int = 1


def _resolved(targets: list[str], by: str, ambiguity: int = 1) -> Resolution:
    return Resolution("resolved", list(dict.fromkeys(targets)), by, ambiguity)


class _Backend:
    name = ""

    def resolve(self, site: CallSite, fn: FunctionDef) -> Resolution | None:
        raise NotImplementedError

    def class_call(self, symtab: SymbolTable, class_qn: str) -> Resolution:
        """Calling a class runs its __init__ (found through the MRO)."""
        init = symtab.constructor(class_qn)
        return _resolved([init], self.name) if init else Resolution("external", by=self.name)


class ScipResolver(_Backend):
    """Precise resolution from a SCIP index produced by `scip-python index`."""

    name = "scip"

    def __init__(self, repo_root: Path, symtab: SymbolTable, index_bytes: bytes) -> None:
        self.symtab = symtab
        self.refs: dict[tuple[str, int], list] = {}
        self.defs: dict[str, tuple[str, int]] = {}
        for doc in decode_index(index_bytes):
            for occ in doc.occurrences:
                if occ.is_definition:
                    self.defs.setdefault(occ.symbol, (doc.path, occ.line))
                else:
                    self.refs.setdefault((doc.path, occ.line), []).append(occ)

    @classmethod
    def create(cls, repo_root: Path, symtab: SymbolTable, scip_bin: str) -> ScipResolver | None:
        index = repo_root / "index.scip"
        if not index.exists():
            exe = shutil.which(scip_bin)
            if exe is None:
                log.info("scip-python not found on PATH; skipping the SCIP resolver")
                return None
            with tempfile.TemporaryDirectory(prefix="scip-") as tmp:
                out = Path(tmp) / "index.scip"
                try:
                    subprocess.run([exe, "index", ".", "--project-name", symtab.g.repo_name, "--output", str(out)],
                                   cwd=repo_root, check=True, capture_output=True, timeout=1800)
                    data = out.read_bytes()
                except (subprocess.SubprocessError, OSError) as exc:
                    log.warning("scip-python failed (%s); skipping the SCIP resolver", exc)
                    return None
            return cls(repo_root, symtab, data)
        return cls(repo_root, symtab, index.read_bytes())

    def resolve(self, site: CallSite, fn: FunctionDef) -> Resolution | None:
        if site.name is None:
            return None
        occs = self.refs.get((site.file_path, site.line - 1), [])
        occ = next((o for o in occs if o.start == site.col), None)
        if occ is None:  # positions may be UTF-16 based; fall back to the identifier length
            occ = next((o for o in occs if o.end - o.start == len(site.name) and abs(o.start - site.col) <= 2), None)
        if occ is None or occ.symbol.startswith("local "):
            return None
        location = self.defs.get(occ.symbol)
        if location is None:  # defined outside the indexed documents
            return Resolution("external", by=self.name)
        path, line0 = location
        qn = self.symtab.fn_by_location.get((path, line0 + 1))
        if qn:
            return _resolved([qn], self.name)
        cqn = self.symtab.class_by_location.get((path, line0 + 1))
        if cqn:
            return self.class_call(self.symtab, cqn)
        return None


class JediResolver(_Backend):
    """Static inference with Jedi: follows imports, `self`, return types and simple assignments."""

    name = "jedi"

    def __init__(self, repo_root: Path, symtab: SymbolTable, source_roots: list[str]) -> None:
        import jedi

        self.jedi = jedi
        self.root = repo_root.resolve()
        self.symtab = symtab
        # Repository source roots go first so the repo's own package wins over an installed copy.
        roots = [str((self.root / r).resolve()) for r in source_roots]
        env_paths = [p for p in sys.path if p and Path(p).resolve() != Path.cwd().resolve()]
        self.project = jedi.Project(path=str(self.root), sys_path=roots + env_paths, smart_sys_path=True)
        self._script_path: str | None = None
        self._script = None

    def _get_script(self, rel_path: str):
        if rel_path != self._script_path:
            path = self.root / rel_path
            self._script = self.jedi.Script(code=path.read_text("utf-8", errors="replace"), path=str(path),
                                            project=self.project)
            self._script_path = rel_path
        return self._script

    def _rel(self, module_path) -> str | None:
        try:
            return Path(module_path).resolve().relative_to(self.root).as_posix()
        except ValueError:
            return None

    def resolve(self, site: CallSite, fn: FunctionDef) -> Resolution | None:
        if site.name is None:
            return None
        try:
            names = self._get_script(site.file_path).goto(site.line, site.col, follow_imports=True)
        except Exception as exc:  # Jedi raises on odd syntax; treat as "no opinion"
            log.debug("jedi failed at %s:%s: %s", site.file_path, site.line, exc)
            return None
        targets: list[str] = []
        external = False
        for n in names:
            rel = self._rel(n.module_path) if n.module_path is not None else None
            if rel is None or n.in_builtin_module():
                external = True
                continue
            if n.type == "function":
                qn = self.symtab.fn_by_location.get((rel, n.line))
                if qn:
                    targets.append(qn)
            elif n.type == "class":
                cqn = self.symtab.class_by_location.get((rel, n.line))
                if cqn:
                    res = self.class_call(self.symtab, cqn)
                    targets.extend(res.targets)
                    external = external or res.status == "external"
        if targets:
            return _resolved(targets, self.name)
        if external:
            return Resolution("external", by=self.name)
        return None


class ImportResolver(_Backend):
    """Resolution through the import table: local defs, imports (with aliases and re-exports),
    module attributes, self/cls/super() method lookup through the class hierarchy, and Class().method."""

    name = "import"

    def __init__(self, symtab: SymbolTable) -> None:
        self.symtab = symtab
        self._resolving: set[tuple[str, str]] = set()  # guards `x = x.copy()` and mutual assignments

    def resolve(self, site: CallSite, fn: FunctionDef) -> Resolution | None:
        if site.name is None:
            return None
        st = self.symtab
        if site.receiver_call is not None:
            if site.receiver_call == "super":
                if not fn.self_class:
                    return None
                method = st.lookup_method(fn.self_class, site.name, skip_self=True)
                if method:
                    return _resolved([method], self.name)
                return Resolution("external", by=self.name) if st.has_external_base(fn.self_class) else None
            instance = self.instance_of(self.resolve_dotted(site.receiver_call, fn))
            if instance is None or instance.kind == "external":
                return Resolution("external", by=self.name) if instance is not None else None
            return self._finish(st.member(instance, site.name))

        parts = site.callee.split(".")
        if not all(p.isidentifier() for p in parts):
            return None
        if fn.self_class and fn.self_name and parts[0] == fn.self_name:
            if len(parts) != 2:
                return None  # self.attr.method(): the attribute's type is unknown
            method = st.lookup_method(fn.self_class, parts[1])
            if method:
                return _resolved([method], self.name)
            return Resolution("external", by=self.name) if st.has_external_base(fn.self_class) else None
        return self._finish(self.resolve_dotted(site.callee, fn))

    def resolve_dotted(self, dotted: str, fn: FunctionDef) -> Target | None:
        parts = dotted.split(".")
        if not all(p.isidentifier() for p in parts):
            return None
        target = self._resolve_head(parts[0], fn)
        for part in parts[1:]:
            if target is None:
                return None
            target = self.symtab.member(target, part)
        return target

    def _resolve_head(self, head: str, fn: FunctionDef) -> Target | None:
        """Look `head` up through the enclosing function scopes, then the module."""
        st = self.symtab
        scope: FunctionDef | None = fn
        while scope is not None:
            if head in scope.nested_defs:
                qn = scope.nested_defs[head]
                return Target("class" if qn in st.g.classes else "function", qn)
            for imp in reversed(scope.local_imports):
                if imp.bound_name == head:
                    return st.resolve_import(imp)
            if head in scope.local_names:
                calls = scope.assigned_calls.get(head, [])
                key = (scope.qualified_name, head)
                if len(calls) == 1 and calls[0] != "?" and key not in self._resolving:
                    self._resolving.add(key)  # x = Foo(...) / x = make_foo() assigned exactly once
                    try:
                        return self.instance_of(self.resolve_dotted(calls[0], scope))
                    finally:
                        self._resolving.discard(key)
                return None  # a parameter or a variable of unknown type
            scope = st.g.functions.get(scope.parent_qn) if scope.parent_qn else None
        return st.resolve_name(fn.module, head)

    def instance_of(self, called: Target | None) -> Target | None:
        """Type of the value produced by calling `called`: a class gives an instance, so does a function whose
        returned class was inferred. External callables give external values."""
        if called is None:
            return None
        if called.kind == "class":
            return Target("instance", called.name)
        if called.kind == "function":
            fn = self.symtab.g.functions.get(called.name)
            return Target("instance", fn.returns) if fn is not None and fn.returns else None
        return called if called.kind == "external" else None

    def _finish(self, target: Target | None) -> Resolution | None:
        if target is None:
            return None
        if target.kind == "function":
            return _resolved([target.name], self.name)
        if target.kind == "class":
            return self.class_call(self.symtab, target.name)
        if target.kind == "external":
            return Resolution("external", by=self.name)
        return None


class NameResolver(_Backend):
    """Last resort: match the bare called name against repository definitions.

    Attribute calls only match methods, bare calls only match top-level functions and classes, names of
    built-in type methods (get, items, append...) are skipped, and more than MAX_CANDIDATES matches is
    treated as unresolved. These edges stay in the graph for retrieval but never become gold."""

    name = "name"
    MAX_CANDIDATES = 3

    def __init__(self, symtab: SymbolTable) -> None:
        self.symtab = symtab

    def resolve(self, site: CallSite, fn: FunctionDef) -> Resolution | None:
        if site.name is None:
            return None
        st = self.symtab
        if "." in site.callee or site.receiver_call:
            if site.name in BUILTIN_TYPE_METHODS:
                return None
            candidates = list(st.methods_by_name.get(site.name, []))
        else:
            candidates = list(st.toplevel_functions_by_name.get(site.name, []))
            for cqn in st.classes_by_name.get(site.name, []):
                init = st.constructor(cqn)
                if init:
                    candidates.append(init)
            if not candidates and site.name in BUILTIN_NAMES:
                return Resolution("external", by=self.name)
        candidates = list(dict.fromkeys(candidates))
        if not candidates or len(candidates) > self.MAX_CANDIDATES:
            return None
        return _resolved(candidates, self.name, ambiguity=len(candidates))


class CallResolver:
    def __init__(self, graph: CodeGraph, symtab: SymbolTable, repo_root: Path, order: list[str],
                 scip_bin: str = "scip-python") -> None:
        self.graph = graph
        self.backends: list[_Backend] = []
        for name in order:
            if name == "scip":
                scip = ScipResolver.create(repo_root, symtab, scip_bin)
                if scip:
                    self.backends.append(scip)
            elif name == "jedi":
                try:
                    self.backends.append(JediResolver(repo_root, symtab, graph.source_roots))
                except ImportError:
                    log.warning("jedi is not installed; skipping the Jedi resolver")
            elif name == "import":
                self.backends.append(ImportResolver(symtab))
            elif name == "name":
                self.backends.append(NameResolver(symtab))

    @property
    def active(self) -> list[str]:
        return [b.name for b in self.backends]

    def run(self, progress: Callable[[str], None] | None = None) -> dict:
        g = self.graph
        counts: Counter[str] = Counter()
        sites = sorted(g.call_sites, key=lambda s: (s.file_path, s.line, s.col))
        for i, site in enumerate(sites):
            if progress and i % 2000 == 0:
                progress(f"resolving call sites {i}/{len(sites)}")
            fn = g.functions[site.caller]
            outcome: Resolution | None = None
            for backend in self.backends:
                outcome = backend.resolve(site, fn)
                if outcome is not None:
                    break
            if outcome is not None and outcome.status == "resolved":
                for target in outcome.targets:
                    g.add_call(site.caller, target, outcome.by, site.line, outcome.ambiguity)
                counts[outcome.by] += 1
            elif outcome is not None:
                counts["external"] += 1
            else:
                counts["dynamic" if site.name is None else "unresolved"] += 1
                g.unresolved[site.caller].append(site.name or site.callee)
        total = len(sites)
        internal = total - counts["external"]
        left = counts["unresolved"] + counts["dynamic"]
        return {
            "call_sites": total,
            "resolvers": self.active,
            "by_resolver": {k: counts[k] for k in ("scip", "jedi", "import", "name") if counts[k]},
            "external": counts["external"],
            "unresolved": counts["unresolved"],
            "dynamic": counts["dynamic"],
            "unresolved_call_rate": round(left / internal, 4) if internal else 0.0,
            "name_only_rate": round(counts["name"] / internal, 4) if internal else 0.0,
        }
