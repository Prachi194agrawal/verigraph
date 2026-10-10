"""Execution check (stretch goal): put generated code in place of the original function and run only the
tests that reach it, inside Docker with no network. Generated code never runs outside this sandbox.

Per build, an image `<prefix>:<build_id>` is built once with the repository and its dependencies installed
(editable install at /repo). Each run copies the checkout, swaps the function in, and mounts the copy at
/repo so the editable install imports the modified code.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import textwrap
from dataclasses import dataclass
from pathlib import Path

from app.config import Settings

_SUMMARY = re.compile(r"(\d+) (passed|failed|error|errors)")
_DOCKERFILE = """FROM python:3.12-slim
RUN pip install --no-cache-dir pytest
COPY . /repo
WORKDIR /repo
RUN pip install --no-cache-dir -e . || true
RUN for f in requirements.txt requirements-dev.txt requirements-test.txt; do \\
      if [ -f "$f" ]; then pip install --no-cache-dir -r "$f" || true; fi; done
"""


@dataclass
class SandboxResult:
    status: str  # passed | failed | error | skipped
    tests_run: int = 0
    passed: int = 0
    failed: int = 0
    log_path: str | None = None
    detail: str = ""


def replace_function(path: Path, def_line: int, end_line: int, code: str) -> None:
    """Replace lines def_line..end_line (1-based) with `code`, keeping the original decorators and indentation."""
    lines = path.read_text("utf-8").splitlines(keepends=True)
    original = lines[def_line - 1]
    indent = original[: len(original) - len(original.lstrip())]
    body = textwrap.dedent(code).strip("\n").splitlines()
    while body and body[0].lstrip().startswith("@"):  # decorators stay as in the original file
        body.pop(0)
    new = [indent + ln if ln.strip() else "\n" for ln in body]
    new = [ln if ln.endswith("\n") else ln + "\n" for ln in new]
    path.write_text("".join(lines[: def_line - 1] + new + lines[end_line:]), "utf-8")


def _ensure_image(checkout: Path, build_id: int, settings: Settings) -> str:
    tag = f"{settings.sandbox_image_prefix}:{build_id}"
    if subprocess.run(["docker", "image", "inspect", tag], capture_output=True).returncode == 0:
        return tag
    with tempfile.TemporaryDirectory() as tmp:
        ctx = Path(tmp) / "ctx"
        shutil.copytree(checkout, ctx, ignore=shutil.ignore_patterns(".git"))
        (ctx / "Dockerfile.verigraph").write_text(_DOCKERFILE)
        subprocess.run(["docker", "build", "-q", "-t", tag, "-f", str(ctx / "Dockerfile.verigraph"), str(ctx)],
                       check=True, capture_output=True, timeout=1800)
    return tag


def run_tests(checkout: Path, build_id: int, meta: dict, code: str, log_name: str, settings: Settings) -> SandboxResult:
    tests = meta.get("tests") or []
    if not tests:
        return SandboxResult("skipped", detail="no tests reach this function")
    if shutil.which("docker") is None:
        return SandboxResult("skipped", detail="docker is not available")
    log_dir = settings.data_dir / "sandbox" / str(build_id)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{log_name}.log"
    try:
        image = _ensure_image(checkout, build_id, settings)
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "repo"
            shutil.copytree(checkout, work, ignore=shutil.ignore_patterns(".git"))
            replace_function(work / meta["file"], meta["def_line"], meta["end_line"], code)
            proc = subprocess.run(
                ["docker", "run", "--rm", "--network", "none", "--memory", "2g", "--cpus", "2",
                 "-v", f"{work}:/repo", "-w", "/repo", image,
                 "python", "-m", "pytest", "-q", "-p", "no:cacheprovider", *tests],
                capture_output=True, text=True, timeout=settings.sandbox_timeout_s)
    except subprocess.TimeoutExpired:
        log_path.write_text("timeout")
        return SandboxResult("error", log_path=str(log_path), detail="timeout")
    except (subprocess.CalledProcessError, OSError) as exc:
        log_path.write_text(str(exc))
        return SandboxResult("error", log_path=str(log_path), detail=str(exc)[:500])
    output = proc.stdout + proc.stderr
    log_path.write_text(output)
    counts = {kind: int(n) for n, kind in _SUMMARY.findall(output)}
    passed = counts.get("passed", 0)
    failed = counts.get("failed", 0) + counts.get("error", 0) + counts.get("errors", 0)
    status = "passed" if proc.returncode == 0 and passed else ("failed" if passed + failed else "error")
    return SandboxResult(status, passed + failed, passed, failed, str(log_path))
