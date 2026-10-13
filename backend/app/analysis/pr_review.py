"""Offline PR impact reports from immutable Git snapshots. No target code is executed."""

from __future__ import annotations

import io
import re
import subprocess
import tarfile
import tempfile
from pathlib import Path
from urllib.parse import quote

from app.analysis.impact import analyze_impact
from app.config import get_settings
from app.eval.index import BuildIndex, index_rows_from_graph
from app.graph.builder import build_structure
from app.ingest.limits import check_checkout_limits


def _git(repo: Path, *args: str, max_bytes: int = 5_000_000) -> bytes:
    # Spool to disk, then bound reads: a large diff/archive must not exhaust analyzer memory.
    with tempfile.TemporaryFile() as output:
        subprocess.run(["git", "-C", str(repo), *args], check=True, stdout=output,
                       stderr=subprocess.PIPE, timeout=180)
        if output.tell() > max_bytes:
            raise ValueError("Git output exceeds the review size limit")
        output.seek(0)
        return output.read()


def _snapshot(repo: Path, commit: str, root: Path) -> None:
    settings = get_settings()
    archive = _git(repo, "archive", "--format=tar", commit,
                   max_bytes=settings.max_repo_bytes + settings.max_repo_files * 2048)
    size = count = 0
    with tarfile.open(fileobj=io.BytesIO(archive)) as entries:
        for entry in entries:
            target = (root / entry.name).resolve()
            if not target.is_relative_to(root.resolve()):
                raise ValueError("Unsafe archive path")
            if not entry.isfile():  # skip links, devices and directories; parents are created below
                continue
            size += entry.size
            count += 1
            if size > settings.max_repo_bytes or count > settings.max_repo_files:
                raise ValueError("PR snapshot exceeds configured repository limits")
            target.parent.mkdir(parents=True, exist_ok=True)
            with entries.extractfile(entry) as source, target.open("wb") as dest:
                dest.write(source.read())


def review(repo: Path, base: str, head: str, github_repo: str | None = None, max_hops: int = 3) -> dict:
    if github_repo and not re.fullmatch(r"[\w.-]+/[\w.-]+", github_repo):
        raise ValueError("GitHub repository must be owner/name")
    base_sha = _git(repo, "rev-parse", "--verify", "--end-of-options", f"{base}^{{commit}}").decode().strip()
    head_sha = _git(repo, "rev-parse", "--verify", "--end-of-options", f"{head}^{{commit}}").decode().strip()
    merge_base = _git(repo, "merge-base", base_sha, head_sha).decode().strip()
    diff = _git(repo, "diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--unified=0",
                merge_base, head_sha, "--").decode("utf-8", errors="replace")
    with tempfile.TemporaryDirectory(prefix="verigraph-pr-") as directory:
        root = Path(directory)
        _snapshot(repo, merge_base, root)
        check_checkout_limits(root, get_settings())
        graph = build_structure(root, github_repo or repo.name, str(repo), merge_base, ["import", "name"])
        index = BuildIndex.from_rows(index_rows_from_graph(graph))
        report = analyze_impact(index, diff=diff, max_hops=max_hops)
    markdown = report.to_markdown("VeriGraph PR impact review", graph.stats.get("unresolved_call_rate"))
    markdown += f"\nBase: `{merge_base}` · Head: `{head_sha}`\n"
    markdown += "\nAnalysis uses the PR merge base and conservative static call resolution. No LLM or tests were run.\n"
    evidence = []
    for kind, rows in (("changed", report.changed), ("caller", report.affected), ("test", report.tests)):
        for row in rows:
            line = row.get("start_line")
            path = row["file_path"]
            url = f"https://github.com/{github_repo}/blob/{merge_base}/{quote(path, safe='/')}"
            if line:
                url += f"#L{line}"
            evidence.append({"kind": kind, **row, "url": url if github_repo else None})
    if evidence:
        markdown += "\n**Source evidence at the analyzed base commit**\n\n| Kind | Symbol | Source |\n|---|---|---|\n"
        for row in evidence[:50]:
            label = f"{row['file_path']}:{row.get('start_line') or '?'}"
            source = f"[{label}]({row['url']})" if row["url"] else f"`{label}`"
            markdown += f"| {row['kind']} | `{row['qualified_name']}` | {source} |\n"
        if len(evidence) > 50:
            markdown += "\nSource table limited to 50 entries; the JSON artifact contains the full list.\n"
    # New files have no old-side line numbers; make their absence from the base explicit.
    added = _git(repo, "diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--diff-filter=A", "--name-only", "-z",
                 merge_base, head_sha, "--").decode("utf-8", errors="replace").strip("\0").split("\0")
    added = [p for p in added if p]
    if added:
        markdown += "\n**New files (not present in the base graph; impact unverifiable)**\n\n"
        markdown += "\n".join(f"- `{p}`" for p in added[:50]) + "\n"
    return {**report.to_dict(), "base_sha": merge_base, "target_base_sha": base_sha, "head_sha": head_sha,
            "added_files": added, "evidence": evidence, "markdown": markdown,
            "analysis": "static", "llm_used": False}
