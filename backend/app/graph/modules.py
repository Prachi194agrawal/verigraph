"""Find the Python files of a repository and give each one its importable module name."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

SKIP_DIRS = {
    ".git", ".hg", ".svn", ".tox", ".nox", ".venv", "venv", "env", "node_modules", "__pycache__",
    "build", "dist", ".eggs", "site-packages", ".mypy_cache", ".pytest_cache", ".ruff_cache",
}
MAX_FILE_BYTES = 1_000_000  # larger files are almost always generated
TEST_DIR_NAMES = {"tests", "test", "testing"}


@dataclass(frozen=True)
class ModuleInfo:
    name: str  # dotted import name, e.g. "requests.sessions"
    is_package: bool  # True for __init__.py
    root: str  # directory the import name is relative to ("." for the repo root)


def discover_python_files(root: Path) -> list[str]:
    """Repository-relative POSIX paths of every .py file worth parsing, sorted."""
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".") and not d.endswith(".egg-info")
        )
        for filename in filenames:
            if not filename.endswith(".py"):
                continue
            full = Path(dirpath) / filename
            try:
                if full.stat().st_size > MAX_FILE_BYTES or full.is_symlink():
                    continue
            except OSError:
                continue
            found.append(full.relative_to(root).as_posix())
    return sorted(found)


def is_test_path(path: str) -> bool:
    parts = path.split("/")
    base = parts[-1]
    return (
        base.startswith("test_")
        or base.endswith("_test.py")
        or base == "conftest.py"
        or any(p in TEST_DIR_NAMES for p in parts[:-1])
    )


def assign_module_names(paths: list[str]) -> dict[str, ModuleInfo]:
    """Module name = path relative to the first ancestor directory that is not a package.

    `src/requests/sessions.py` -> `requests.sessions` (src/ has no __init__.py), which is how Python
    itself would import it. Names that collide (two scripts called `conf.py`) fall back to the full
    dotted path so every module name stays unique.
    """
    path_set = set(paths)

    def is_package_dir(d: PurePosixPath) -> bool:
        return str(d) != "." and f"{d}/__init__.py" in path_set

    infos: dict[str, ModuleInfo] = {}
    for p in paths:
        pp = PurePosixPath(p)
        is_package = pp.name == "__init__.py"
        parts: list[str] = [] if is_package else [pp.stem]
        d = pp.parent
        if is_package:
            parts.insert(0, d.name)
            d = d.parent
        while is_package_dir(d):
            parts.insert(0, d.name)
            d = d.parent
        name = ".".join(x for x in parts if x) or pp.stem
        infos[p] = ModuleInfo(name=name, is_package=is_package, root=str(d))

    by_name: dict[str, list[str]] = {}
    for p, info in infos.items():
        by_name.setdefault(info.name, []).append(p)
    for name, ps in by_name.items():
        if len(ps) < 2:
            continue
        for p in ps:
            pp = PurePosixPath(p)
            parts = list(pp.parent.parts) if pp.name == "__init__.py" else [*pp.parent.parts, pp.stem]
            infos[p] = ModuleInfo(name=".".join(parts) or name, is_package=infos[p].is_package, root=".")
    return infos


def resolve_relative(module: str, is_package: bool, level: int, dotted: str) -> str | None:
    """Absolute module for `from <level dots><dotted> import ...` written inside `module`."""
    parts = module.split(".")
    package = parts if is_package else parts[:-1]
    up = level - 1
    if up > len(package):
        return None
    base = package[: len(package) - up] if up else package
    if dotted:
        base = [*base, *dotted.split(".")]
    return ".".join(base) or None
