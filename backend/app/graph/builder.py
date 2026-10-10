"""Build the structure layer of the code graph for one checked-out commit."""

from __future__ import annotations

import logging
import sys
from collections import Counter
from collections.abc import Callable
from pathlib import Path

from app.config import PRECISE_RESOLVERS
from app.graph.model import CodeGraph
from app.graph.modules import assign_module_names, discover_python_files
from app.graph.parser import PythonParser
from app.graph.resolver import CallResolver, ImportResolver
from app.graph.symbols import SymbolTable

log = logging.getLogger(__name__)
STDLIB = frozenset(getattr(sys, "stdlib_module_names", ()))


def parse_repository(repo_root: Path, repo_name: str, repo_url: str, commit_sha: str | None,
                     progress: Callable[[str], None] | None = None) -> CodeGraph:
    """Step 2a: parse every Python file into files, modules, classes, functions, imports and call sites."""
    paths = discover_python_files(repo_root)
    infos = assign_module_names(paths)
    graph = CodeGraph(repo_name=repo_name, repo_url=repo_url, commit_sha=commit_sha)
    parser = PythonParser()
    for i, path in enumerate(paths):
        if progress and i % 200 == 0:
            progress(f"parsing files {i}/{len(paths)}")
        info = infos[path]
        parsed = parser.parse((repo_root / path).read_bytes(), path, info.name, info.is_package)
        graph.files[path] = parsed.file
        graph.modules[info.name] = path
        for cls in parsed.classes:
            graph.classes.setdefault(cls.qualified_name, cls)
        for fn in parsed.functions:
            graph.functions.setdefault(fn.qualified_name, fn)
        graph.call_sites.extend(s for s in parsed.call_sites if s.caller in graph.functions)
    graph.source_roots = sorted({info.root for info in infos.values()})
    return graph


def resolve_bases(graph: CodeGraph, symtab: SymbolTable) -> None:
    """INHERITS edges: base-class expressions resolved through imports, then by unique class name."""
    for cls in graph.classes.values():
        for base in cls.bases:
            if base == "object":
                continue
            target = symtab.resolve_dotted(cls.module, base)
            if target is not None and target.kind == "class" and target.name != cls.qualified_name:
                graph.inherits[(cls.qualified_name, target.name)] = "import"
                continue
            if target is None:
                candidates = [c for c in symtab.classes_by_name.get(base.split(".")[-1], []) if c != cls.qualified_name]
                if len(candidates) == 1:
                    graph.inherits[(cls.qualified_name, candidates[0])] = "name"
                    continue
            graph.external_bases[cls.qualified_name].append(base)
    symtab.refresh_hierarchy()


def infer_returns(graph: CodeGraph, symtab: SymbolTable) -> None:
    """Function.returns = the one class every `return X(...)` / `return self` produces (factories, fluent APIs)."""
    resolver = ImportResolver(symtab)
    for fn in graph.functions.values():
        if not fn.return_exprs:
            continue
        classes: set[str] = set()
        for expr in fn.return_exprs:
            if expr == "<self>" or (fn.kind == "classmethod" and expr == fn.self_name):
                cls = fn.self_class
            elif expr == "?":
                cls = None
            else:
                target = resolver.resolve_dotted(expr, fn)
                cls = target.name if target is not None and target.kind == "class" else None
            if cls is None:
                break
            classes.add(cls)
        else:
            if len(classes) == 1:
                fn.returns = classes.pop()


def link_imports(graph: CodeGraph, symtab: SymbolTable) -> None:
    """IMPORTS edges: File -> Module for repository modules, File -> ExternalPackage otherwise."""
    local_imports: dict[str, list] = {}
    for fn in graph.functions.values():
        local_imports.setdefault(fn.file_path, []).extend(fn.local_imports)
    for f in graph.files.values():
        for imp in [*f.imports, *local_imports.get(f.path, [])]:
            if imp.module is None:
                continue
            module = imp.module
            if imp.is_from and imp.name not in (None, "*") and symtab.is_module(f"{module}.{imp.name}"):
                module = f"{module}.{imp.name}"
            # Link to the closest module that has a file (namespace packages have none).
            probe = module
            while probe and probe not in graph.modules and symtab.is_module(probe):
                probe = probe.rpartition(".")[0]
            if probe in graph.modules:
                graph.file_imports.setdefault((f.path, "module", probe), imp.line)
            elif not symtab.is_module(module):
                package = module.split(".")[0]
                graph.external_packages[package] = package in STDLIB
                graph.file_imports.setdefault((f.path, "external", package), imp.line)


def link_tests(graph: CodeGraph) -> None:
    """TESTS edges: from each test function to the non-test functions it calls directly (precise edges)."""
    for (caller, callee), edge in graph.calls.items():
        src, dst = graph.functions[caller], graph.functions[callee]
        if src.is_test and not graph.files[dst.file_path].is_test and edge.resolved_by in PRECISE_RESOLVERS:
            graph.tests[(caller, callee)] = edge.resolved_by


def build_structure(repo_root: Path, repo_name: str, repo_url: str, commit_sha: str | None,
                    resolvers: list[str], scip_bin: str = "scip-python",
                    progress: Callable[[str], None] | None = None) -> CodeGraph:
    """Steps 2a-2c: parse, resolve, and link. History and meaning are added later."""
    graph = parse_repository(repo_root, repo_name, repo_url, commit_sha, progress)
    symtab = SymbolTable(graph)
    resolve_bases(graph, symtab)
    infer_returns(graph, symtab)
    link_imports(graph, symtab)
    resolver = CallResolver(graph, symtab, repo_root, resolvers, scip_bin)
    call_stats = resolver.run(progress)
    link_tests(graph)
    for f in graph.files.values():
        f.namespace = symtab.namespace(f.module)
    graph.stats = structure_stats(graph, call_stats)
    return graph


def structure_stats(graph: CodeGraph, call_stats: dict) -> dict:
    edges_by_resolver = Counter(e.resolved_by for e in graph.calls.values())
    return {
        "files": len(graph.files),
        "test_files": sum(f.is_test for f in graph.files.values()),
        "modules": len(graph.modules),
        "classes": len(graph.classes),
        "functions": len(graph.functions),
        "test_functions": sum(fn.is_test for fn in graph.functions.values()),
        "parse_errors": sum(f.parse_errors for f in graph.files.values()),
        "calls_edges": len(graph.calls),
        "calls_edges_by_resolver": dict(edges_by_resolver),
        "inherits_edges": len(graph.inherits),
        "tests_edges": len(graph.tests),
        "imports_edges": len(graph.file_imports),
        "external_packages": len(graph.external_packages),
        **call_stats,
    }
