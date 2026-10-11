"""Language detection and bounded source discovery for mixed repositories.

Structural support is deliberately separate from file coverage: detecting a language does not
claim that we can resolve its runtime dispatch, frameworks, or cross-language interfaces.
"""

from __future__ import annotations

import codecs
import os
from dataclasses import dataclass
from pathlib import Path

from app.graph.modules import MAX_FILE_BYTES, SKIP_DIRS


@dataclass(frozen=True)
class LanguageSpec:
    name: str
    extensions: tuple[str, ...]
    grammar: str | None
    level: str = "structural"


LANGUAGES = (
    LanguageSpec("python", (".py", ".pyi"), "python", "semantic"),
    LanguageSpec("javascript", (".js", ".jsx", ".mjs", ".cjs"), "javascript"),
    LanguageSpec("typescript", (".ts", ".mts", ".cts"), "typescript"),
    LanguageSpec("tsx", (".tsx",), "tsx"),
    LanguageSpec("java", (".java",), "java"),
    LanguageSpec("go", (".go",), "go"),
    LanguageSpec("rust", (".rs",), "rust"),
    LanguageSpec("c", (".c", ".h"), "c"),
    LanguageSpec("cpp", (".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx"), "cpp"),
    LanguageSpec("csharp", (".cs",), "csharp"),
    LanguageSpec("ruby", (".rb", ".rake"), "ruby"),
    LanguageSpec("php", (".php",), "php"),
    LanguageSpec("kotlin", (".kt", ".kts"), "kotlin"),
    LanguageSpec("swift", (".swift",), "swift"),
    LanguageSpec("scala", (".scala", ".sc"), "scala"),
    LanguageSpec("lua", (".lua",), "lua"),
    LanguageSpec("bash", (".sh", ".bash"), "bash"),
    LanguageSpec("r", (".r", ".R"), None, "file"),
    LanguageSpec("dart", (".dart",), None, "file"),
    LanguageSpec("elixir", (".ex", ".exs"), None, "file"),
    LanguageSpec("erlang", (".erl", ".hrl"), None, "file"),
    LanguageSpec("haskell", (".hs", ".lhs"), None, "file"),
    LanguageSpec("ocaml", (".ml", ".mli"), None, "file"),
    LanguageSpec("clojure", (".clj", ".cljs", ".cljc"), None, "file"),
    LanguageSpec("julia", (".jl",), None, "file"),
    LanguageSpec("perl", (".pl", ".pm"), None, "file"),
    LanguageSpec("fortran", (".f", ".f90", ".f95"), None, "file"),
    LanguageSpec("objective_c", (".m", ".mm"), None, "file"),
    LanguageSpec("solidity", (".sol",), None, "file"),
    LanguageSpec("zig", (".zig",), None, "file"),
    LanguageSpec("vue", (".vue",), None, "file"),
    LanguageSpec("svelte", (".svelte",), None, "file"),
    LanguageSpec("html", (".html", ".htm"), None, "file"),
    LanguageSpec("css", (".css", ".scss", ".sass", ".less"), None, "file"),
    LanguageSpec("sql", (".sql",), None, "file"),
    LanguageSpec("protobuf", (".proto",), None, "file"),
    LanguageSpec("powershell", (".ps1", ".psm1"), None, "file"),
    LanguageSpec("yaml", (".yaml", ".yml"), None, "file"),
    LanguageSpec("json", (".json", ".jsonc"), None, "file"),
    LanguageSpec("toml", (".toml",), None, "file"),
    LanguageSpec("markdown", (".md", ".mdx", ".rst"), None, "file"),
)
BY_EXTENSION = {extension.lower(): spec for spec in LANGUAGES for extension in spec.extensions}
LANGUAGE_SPECS = LANGUAGES
ADDITIONAL_SKIP_DIRS = {"vendor", "target", "coverage", ".next", "out", "bin", "obj", "Pods"}
BINARY_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip", ".gz", ".tar", ".bz2",
    ".xz", ".7z", ".whl", ".pyc", ".pyo", ".so", ".dll", ".exe", ".o", ".a", ".class",
    ".jar", ".woff", ".woff2", ".ttf", ".mp3", ".mp4", ".wav", ".sqlite", ".db", ".bin",
    ".npy", ".npz", ".parquet", ".pkl", ".pickle", ".pt", ".pth", ".onnx", ".lock",
}
GENERATED_LOCKFILES = {"package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml",
                       "poetry.lock", "uv.lock", "Cargo.lock", "composer.lock", "Gemfile.lock"}


def detect_language(path: str) -> LanguageSpec | None:
    p = Path(path)
    extension = p.suffix.lower()
    if p.name in GENERATED_LOCKFILES or extension in BINARY_EXTENSIONS or p.name.startswith("."):
        return None
    known = BY_EXTENSION.get(extension)
    if known:
        return known
    # Unknown extensions are honest inventory, never advertised as parsed code languages.
    if extension:
        return LanguageSpec("other", (extension,), None, "file")
    if p.name in {"Dockerfile", "Makefile", "CMakeLists.txt", "LICENSE", "NOTICE", "README", "Gemfile"}:
        return LanguageSpec("other", (), None, "file")
    return None


def is_multilanguage_test_path(path: str) -> bool:
    p = Path(path)
    name = p.name.lower()
    return (
        any(part.lower() in {"test", "tests", "testing", "__tests__", "spec", "specs"} for part in p.parts[:-1])
        or name.startswith("test_")
        or name.endswith(("_test.go", "_spec.rb"))
        or ".test." in name or ".spec." in name
        or p.stem.endswith(("Test", "Tests", "Spec"))
    )


def discover_source_files(root: Path) -> tuple[list[tuple[str, LanguageSpec]], dict[str, int]]:
    """Discover useful sources without following symlinks or reading generated/large files."""
    found: list[tuple[str, LanguageSpec]] = []
    skipped = {"large_files": 0, "symlinks": 0, "unreadable_files": 0}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in SKIP_DIRS | ADDITIONAL_SKIP_DIRS and not d.startswith(".")
            and not d.endswith(".egg-info") and not (Path(dirpath) / d).is_symlink()
        )
        for filename in sorted(filenames):
            full = Path(dirpath) / filename
            path = full.relative_to(root).as_posix()
            spec = detect_language(path)
            if spec is None:
                continue
            if full.is_symlink():
                skipped["symlinks"] += 1
                continue
            try:
                size = full.stat().st_size
            except OSError:
                skipped["unreadable_files"] += 1
                continue
            if size > MAX_FILE_BYTES:
                skipped["large_files"] += 1
                continue
            if spec.name == "other":
                try:
                    with full.open("rb") as handle:
                        sample = handle.read(8192)
                    codecs.getincrementaldecoder("utf-8")().decode(sample, final=False)
                    if any(b < 32 and b not in {9, 10, 13} for b in sample):
                        continue
                except (OSError, UnicodeDecodeError):
                    continue
            # Minified JS and generated protocol bindings are unsuitable graph/benchmark inputs.
            if filename.endswith((".min.js", ".min.css", ".pb.go", "_pb2.py")):
                continue
            found.append((path, spec))
    return sorted(found, key=lambda item: item[0]), skipped


def language_capabilities() -> list[dict]:
    return [{"language": s.name, "extensions": list(s.extensions), "analysis_level": s.level,
             "call_resolution": "python semantic resolvers" if s.name == "python" else
             "conservative scope and explicit imports" if s.grammar else "file inventory only"}
            for s in LANGUAGES]
