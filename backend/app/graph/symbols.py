"""Symbol table: what a name means at the top level of a module, following imports and re-exports.

Used by the import-based call resolver (graph time) and, through each File node's stored namespace, by
the symbol checker that grades generated code (eval time). Both share `ClassHierarchy` for MRO lookups.
"""

from __future__ import annotations

import builtins
from collections import defaultdict
from dataclasses import dataclass

from app.graph.model import CodeGraph, ImportRecord

BUILTIN_NAMES = frozenset(dir(builtins))
# Method names of built-in types. A bare-name match on `x.get(...)` is too likely to be dict.get.
BUILTIN_TYPE_METHODS = frozenset(
    name for t in (dict, list, str, bytes, set, frozenset, tuple, int, float, object)
    for name in dir(t) if not name.startswith("__")
)
MAX_IMPORT_DEPTH = 8


@dataclass(frozen=True)
class Target:
    kind: str  # module | class | function | instance | value | external
    name: str  # qualified name (module/class/function), class for instances, package for external


class ClassHierarchy:
    """MRO-style method lookup over repository classes. Subclasses fill `_bases`, `_methods`,
    `_attributes` and `_external_bases`, all keyed by class qualified name."""

    def __init__(self) -> None:
        self._bases: dict[str, list[str]] = defaultdict(list)
        self._methods: dict[str, dict[str, str]] = defaultdict(dict)
        self._attributes: dict[str, set[str]] = defaultdict(set)
        self._external_bases: dict[str, list[str]] = defaultdict(list)

    def mro(self, class_qn: str) -> list[str]:
        """Depth-first, left-to-right linearisation without duplicates (C3 is not needed for lookups)."""
        order: list[str] = []
        stack = [class_qn]
        while stack:
            c = stack.pop()
            if c in order:
                continue
            order.append(c)
            stack.extend(reversed(self._bases.get(c, [])))
        return order

    def lookup_method(self, class_qn: str, name: str, skip_self: bool = False) -> str | None:
        for c in self.mro(class_qn)[1 if skip_self else 0:]:
            qn = self._methods.get(c, {}).get(name)
            if qn:
                return qn
        return None

    def has_attribute(self, class_qn: str, name: str) -> bool:
        return any(name in self._attributes.get(c, ()) for c in self.mro(class_qn))

    def has_external_base(self, class_qn: str) -> bool:
        return any(self._external_bases.get(c) for c in self.mro(class_qn))

    def constructor(self, class_qn: str) -> str | None:
        return self.lookup_method(class_qn, "__init__")


class SymbolTable(ClassHierarchy):
    def __init__(self, graph: CodeGraph) -> None:
        super().__init__()
        self.g = graph
        self.module_files = graph.modules
        self._namespace_packages = {m.rsplit(".", i)[0] for m in graph.modules for i in range(1, m.count(".") + 1)}
        self.top_defs: dict[str, dict[str, tuple[str, str]]] = {}
        self.imports: dict[str, list[ImportRecord]] = {}
        self.assignments: dict[str, set[str]] = {}
        for f in graph.files.values():
            self.top_defs[f.module] = f.top_level
            self.imports[f.module] = f.imports
            self.assignments[f.module] = f.module_assignments
        self.fn_by_location: dict[tuple[str, int], str] = {}
        self.class_by_location: dict[tuple[str, int], str] = {}
        self.methods_by_name: dict[str, list[str]] = defaultdict(list)
        self.toplevel_functions_by_name: dict[str, list[str]] = defaultdict(list)
        self.classes_by_name: dict[str, list[str]] = defaultdict(list)
        for fn in graph.functions.values():
            self.fn_by_location[(fn.file_path, fn.name_line)] = fn.qualified_name
            if fn.class_qn:
                self.methods_by_name[fn.name].append(fn.qualified_name)
            elif not fn.is_nested:
                self.toplevel_functions_by_name[fn.name].append(fn.qualified_name)
        for cls in graph.classes.values():
            self.class_by_location[(cls.file_path, cls.name_line)] = cls.qualified_name
            self.classes_by_name[cls.name].append(cls.qualified_name)
            self._methods[cls.qualified_name] = cls.methods
            self._attributes[cls.qualified_name] = cls.attributes
        self.refresh_hierarchy()

    def refresh_hierarchy(self) -> None:
        """Re-read INHERITS edges and external bases after base classes have been resolved."""
        self._bases = defaultdict(list)
        for child, parent in self.g.inherits:
            self._bases[child].append(parent)
        self._external_bases = self.g.external_bases

    # -- name resolution -----------------------------------------------------------------------------

    def is_module(self, name: str) -> bool:
        return name in self.module_files or name in self._namespace_packages

    def resolve_name(self, module: str, name: str, depth: int = 0) -> Target | None:
        """What `name` refers to at the top level of `module`."""
        if depth > MAX_IMPORT_DEPTH:
            return None
        defs = self.top_defs.get(module, {})
        if name in defs:
            kind, qn = defs[name]
            return Target(kind, qn)
        imports = self.imports.get(module, [])
        for imp in reversed(imports):
            if imp.name != "*" and imp.bound_name == name:
                return self.resolve_import(imp, depth + 1)
        for imp in imports:
            if imp.name == "*" and imp.module and imp.module in self.module_files:
                found = self.resolve_name(imp.module, name, depth + 1)
                if found is not None:
                    return found
        if name in self.assignments.get(module, set()):
            return Target("value", f"{module}.{name}")
        if name in BUILTIN_NAMES:
            return Target("external", "builtins")
        return None

    def resolve_import(self, imp: ImportRecord, depth: int = 0) -> Target | None:
        if imp.module is None:
            return None
        if not imp.is_from:
            return self.module_target(imp.module if imp.alias else imp.module.split(".")[0])
        sub = f"{imp.module}.{imp.name}"
        if self.is_module(sub):
            return Target("module", sub)
        if self.is_module(imp.module):
            return self.resolve_name(imp.module, imp.name, depth + 1) if imp.module in self.module_files else None
        return Target("external", imp.module.split(".")[0])

    def module_target(self, module: str) -> Target:
        if self.is_module(module):
            return Target("module", module)
        return Target("external", module.split(".")[0])

    def member(self, target: Target, attr: str) -> Target | None:
        """`target.attr`, e.g. a module's function or a class's method."""
        if target.kind == "module":
            sub = f"{target.name}.{attr}"
            if self.is_module(sub):
                return Target("module", sub)
            return self.resolve_name(target.name, attr) if target.name in self.module_files else None
        if target.kind in ("class", "instance"):
            method = self.lookup_method(target.name, attr)
            if method:
                return Target("function", method)
            nested = f"{target.name}.{attr}"
            if nested in self.g.classes:
                return Target("class", nested)
            if self.has_attribute(target.name, attr):
                return Target("value", nested)
            if self.has_external_base(target.name):
                return Target("external", "inherited")
            return None
        if target.kind == "external":
            return target
        return None

    def resolve_dotted(self, module: str, dotted: str) -> Target | None:
        parts = dotted.split(".")
        target = self.resolve_name(module, parts[0])
        for part in parts[1:]:
            if target is None:
                return None
            target = self.member(target, part)
        return target

    def namespace(self, module: str) -> dict[str, list[str]]:
        """Resolved top-level bindings of a module: name -> [kind, target]. Stored on the File node so the
        eval-time symbol checker knows which names generated code may use without importing them."""
        out: dict[str, list[str]] = {}
        for name, (kind, qn) in self.top_defs.get(module, {}).items():
            out[name] = [kind, qn]
        for imp in self.imports.get(module, []):
            bound = imp.bound_name
            if not bound or bound == "*" or bound in out:
                continue
            target = self.resolve_import(imp)
            if target is not None:
                out[bound] = [target.kind, target.name]
        for name in self.assignments.get(module, set()):
            out.setdefault(name, ["value", f"{module}.{name}"])
        return out
