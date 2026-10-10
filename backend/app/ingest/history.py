"""History layer from git: commits, authors, the functions each commit changed, and PR numbers.

Changed functions are found by intersecting diff line numbers with function line ranges, using the same
Tree-sitter parser as the structure layer on the before/after versions of every touched file. That
yields exactly the qualified names used in the graph (PyDriller's lizard-based `changed_methods` names
nested and same-named methods differently, which makes them hard to map onto Function nodes).
"""

from __future__ import annotations

import logging
import re
from bisect import bisect_left, bisect_right
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from app.graph.model import CodeGraph, Commit, PullRequest
from app.graph.parser import PythonParser

log = logging.getLogger(__name__)

PR_SQUASH = re.compile(r"\(#(\d+)\)\s*$")  # "Fix redirects (#6650)" on the title line
PR_MERGE = re.compile(r"^Merge pull request #(\d+)")
FIXES = re.compile(r"\b(?:fix(?:e[sd])?|close[sd]?|resolve[sd]?)\b:?\s+(?:[\w.-]+/[\w.-]+)?#(\d+)", re.IGNORECASE)


def iso_utc(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def parse_fixes(text: str | None) -> set[int]:
    return {int(n) for n in FIXES.findall(text or "")}


def _count_in_range(sorted_lines: list[int], start: int, end: int) -> int:
    return bisect_right(sorted_lines, end) - bisect_left(sorted_lines, start)


def changed_functions(parser: PythonParser, module: str, before: str | None, after: str | None,
                      added: list[int], deleted: list[int]) -> dict[str, list[int]]:
    """qualified name -> [lines added, lines deleted] for every function whose body the diff touches."""
    out: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for source, lines, slot in ((after, sorted(added), 0), (before, sorted(deleted), 1)):
        if not source or not lines:
            continue
        for kind, qn, start, end in parser.definition_ranges(source.encode("utf-8", "replace"), module):
            if kind == "function":
                n = _count_in_range(lines, start, end)
                if n:
                    out[qn][slot] += n
    return dict(out)


def mine_history(repo_path: Path, graph: CodeGraph, max_commits: int,
                 progress: Callable[[str], None] | None = None) -> dict:
    """Walk the newest `max_commits` commits reachable from the pinned HEAD."""
    from git import Repo as GitRepo
    from pydriller import Repository

    git_repo = GitRepo(repo_path)
    # GitHub records a merge into this repository on its first-parent chain. A "Merge pull request #N" that is
    # only reachable through a second parent was made in a contributor's fork (often #1, syncing upstream
    # master into the fork): N is a PR of that fork, and its branch range would pull in unrelated commits.
    try:
        first_parent: set[str] | None = set(git_repo.git.rev_list("--first-parent", "HEAD").split())
    except Exception:  # noqa: BLE001 - shallow or odd histories: keep every merge title
        first_parent = None
    module_of = {path: f.module for path, f in graph.files.items()}
    parser = PythonParser()
    n_modifies = 0
    skipped_fork_merges = 0
    for order, commit in enumerate(Repository(str(repo_path), order="reverse").traverse_commits()):
        if order >= max_commits:
            break
        if progress and order % 100 == 0:
            progress(f"mining history: commit {order}/{max_commits}")
        message = commit.msg or ""
        title = message.splitlines()[0][:300] if message else ""
        c = Commit(
            sha=commit.hash, title=title, message=message[:2000], author_name=commit.author.name or "unknown",
            author_email=(commit.author.email or f"{commit.author.name}@unknown").lower(),
            authored_at=iso_utc(commit.author_date), committed_at=iso_utc(commit.committer_date),
            order=order, is_merge=commit.merge,
        )
        graph.commits[c.sha] = c

        squash, merge = PR_SQUASH.search(title), PR_MERGE.match(title)
        if merge and first_parent is not None and c.sha not in first_parent:
            merge = None
            skipped_fork_merges += 1
        if squash or merge:
            c.pr_number = int((squash or merge).group(1))
            pr = graph.pull_requests.setdefault(c.pr_number, PullRequest(number=c.pr_number))
            pr.commits.add(c.sha)
            pr.fixes |= parse_fixes(message)
            if merge and len(commit.parents) == 2:
                try:
                    branch = git_repo.git.rev_list(f"{c.sha}^1..{c.sha}^2").split()
                except Exception:  # noqa: BLE001 - odd histories (grafts, missing parents)
                    branch = []
                pr.commits.update(branch)

        if commit.merge:  # PyDriller reports no modified files for merges; the branch commits carry them
            continue
        for mf in commit.modified_files:
            path = mf.new_path
            if not path or not path.endswith(".py") or path not in module_of:
                continue  # only files that still exist at the pinned commit can map to Function nodes
            diff = mf.diff_parsed
            changed = changed_functions(parser, module_of[path], mf.source_code_before, mf.source_code,
                                        [n for n, _ in diff["added"]], [n for n, _ in diff["deleted"]])
            for qn, (added, deleted) in changed.items():
                if qn in graph.functions:
                    graph.modifies[(c.sha, qn)] = {"lines_added": added, "lines_deleted": deleted,
                                                   "change_type": mf.change_type.name}
                    n_modifies += 1

    for pr in graph.pull_requests.values():
        # Branch commits may be older than the mined window; "Fixes #N" often sits in a branch commit.
        pr.commits &= graph.commits.keys()
        for sha in pr.commits:
            pr.fixes |= parse_fixes(graph.commits[sha].message)
    return {"commits": len(graph.commits), "modifies_edges": n_modifies,
            "pull_requests_from_git": len(graph.pull_requests), "fork_merges_skipped": skipped_fork_merges}
