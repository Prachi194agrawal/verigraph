"""Localization questions with outside gold: SWE-bench instances (problem statement + gold patch).

A graph build is pinned to one commit, so only instances whose `base_commit` equals the build's commit are
used: build the graph at an instance's base commit, then import that instance. Gold = the non-test functions
whose lines (at the base commit) the gold patch deletes or inserts into.
"""

from __future__ import annotations

import re
from collections import defaultdict

from app.graph.store import GraphStore
from app.testgen.templates import Draft

_HUNK = re.compile(r"^@@ -(\d+)(?:,\d+)? \+\d+(?:,\d+)? @@")
MAX_PROBLEM_CHARS = 2000


def patch_lines(patch: str) -> dict[str, set[int]]:
    """Old-file line numbers touched per file: deleted lines, and the line before each insertion."""
    touched: dict[str, set[int]] = defaultdict(set)
    path: str | None = None
    old = 0
    for line in patch.splitlines():
        if line.startswith("--- "):
            src = line[4:].strip()
            path = None if src == "/dev/null" else src.removeprefix("a/")
            continue
        if line.startswith("+++ "):
            continue
        m = _HUNK.match(line)
        if m:
            old = int(m.group(1))
            continue
        if path is None or line.startswith("\\"):
            continue
        if line.startswith("-"):
            touched[path].add(old)
            old += 1
        elif line.startswith("+"):
            touched[path].add(max(old - 1, 1))
        else:
            old += 1
    return dict(touched)


def swebench_drafts(store: GraphStore, build_id: int, commit_sha: str | None, instances: list[dict]) -> tuple[list[Draft], list[str]]:
    rows = store.read("MATCH (f:Function {build_id: $b}) WHERE f.in_test_file = false RETURN f.file_path AS path, "
                      "f.qualified_name AS qn, f.start_line AS start, f.end_line AS end", {"b": build_id})
    by_file: dict[str, list[tuple[str, int, int]]] = defaultdict(list)
    for r in rows:
        by_file[r["path"]].append((r["qn"], r["start"], r["end"]))
    drafts, skipped = [], []
    for inst in instances:
        iid = inst.get("instance_id", "?")
        base = inst.get("base_commit") or ""
        if commit_sha and base and not commit_sha.startswith(base) and not base.startswith(commit_sha):
            skipped.append(f"{iid}: base commit {base[:10]} != build commit {commit_sha[:10]}")
            continue
        lines = patch_lines(inst.get("patch", ""))
        gold = sorted({qn for path, ls in lines.items() for qn, s, e in by_file.get(path, [])
                       if any(s <= n <= e for n in ls)})
        if not 1 <= len(gold) <= 10:
            skipped.append(f"{iid}: {len(gold)} gold functions")
            continue
        problem = inst.get("problem_statement", "").strip()
        if len(problem) > MAX_PROBLEM_CHARS:
            problem = problem[:MAX_PROBLEM_CHARS] + " [...]"
        text = (f"Here is an issue reported against this repository ({iid}).\n\n{problem}\n\n"
                "Which functions in this repository need to change to fix this issue? "
                "List fully-qualified function names, most likely first.")
        drafts.append(Draft("localization", text, f"// gold patch of SWE-bench instance {iid}",
                            {"build_id": build_id, "instance_id": iid}, gold, None, iid,
                            {"source": "swebench", "instance_id": iid}))
    return drafts, skipped
