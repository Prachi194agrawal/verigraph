"""Change impact from the code graph: which functions and tests a change can affect.

This is the graph answering a developer question directly ("what breaks if I change `parse_headers`?"), with no
LLM in the loop, so the answer is exact up to the graph's own coverage.

Input: a unified diff and/or function names. Diff line numbers are read on the *old* side, so build the graph at
the commit the diff applies to (a PR's base commit). Output:

- changed: the functions whose lines the diff deletes or inserts into (both a nested function and its parent);
- affected: every non-test function that reaches a changed one through precise CALLS edges within `max_hops`;
- tests: the test functions that reach a changed one within `max_hops`, with pytest ids ready to run;
- possibly_affected: callers reachable only through name-matched edges. Those edges are guesses, so they are
  listed separately and never mixed into the precise sets.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from app.eval.index import BuildIndex
from app.eval.normalize import normalize_symbol
from app.testgen.swebench import patch_lines

MAX_LISTED = 50


@dataclass
class ImpactReport:
    max_hops: int
    changed: list[dict] = field(default_factory=list)  # {qualified_name, file_path}
    unmatched: list[str] = field(default_factory=list)  # inputs that map to no function of this build
    affected: list[dict] = field(default_factory=list)  # {qualified_name, file_path, hops}
    tests: list[dict] = field(default_factory=list)  # {qualified_name, pytest_id, hops}
    possibly_affected: list[dict] = field(default_factory=list)  # {qualified_name, file_path, hops}

    def to_dict(self) -> dict:
        return asdict(self)

    def to_markdown(self, title: str = "VeriGraph change impact", unresolved_call_rate: float | None = None) -> str:
        """A summary in GitHub-flavoured Markdown, e.g. for a pull-request comment."""
        out = [f"### {title}", ""]
        if not self.changed:
            out.append("No function of this build is changed. New files, module-level changes and unresolved paths "
                       "may still have an impact; this is not a clean bill of health.")
        else:
            out.append(f"**Changed functions ({len(self.changed)}):** "
                       + ", ".join(f"`{c['qualified_name']}`" for c in self.changed[:MAX_LISTED]))
            hops = f"{self.max_hops} hop" + ("s" if self.max_hops != 1 else "")
            out += ["", f"**Affected callers ({len(self.affected)}, precise call graph, up to {hops})**"]
            if self.affected:
                out += ["", "| function | hops |", "|---|---|"]
                out += [f"| `{a['qualified_name']}` | {a['hops']} |" for a in self.affected[:MAX_LISTED]]
            else:
                out.append("none")
            out += ["", f"**Tests to run ({len(self.tests)})**"]
            ids = [t["pytest_id"] or t["qualified_name"] for t in self.tests[:MAX_LISTED]]
            out += ["", "```", *ids, "```"] if ids else ["none: no test reaches the change through precise edges"]
            if self.possibly_affected:
                out += ["", f"**Possibly affected ({len(self.possibly_affected)}, name-matched edges only):** "
                        + ", ".join(f"`{p['qualified_name']}`" for p in self.possibly_affected[:MAX_LISTED])]
        if self.unmatched:
            out += ["", "Not mapped to a function: " + "; ".join(self.unmatched[:MAX_LISTED])]
        if len(self.affected) > MAX_LISTED or len(self.tests) > MAX_LISTED:
            out += ["", f"Lists are cut at {MAX_LISTED} entries."]
        if unresolved_call_rate:
            out += ["", f"_{unresolved_call_rate:.1%} of this build's calls into repository code are unresolved "
                        "(dynamic dispatch, callbacks), so the real impact can be larger._"]
        out += ["", "_Static impact analysis does not verify runtime behavior. Dynamic dispatch, callbacks, "
                     "cross-language calls and newly added functions may be unverifiable._"]
        return "\n".join(out) + "\n"


def changed_from_diff(index: BuildIndex, diff: str) -> tuple[set[str], list[str]]:
    """Functions whose (old-side) lines a unified diff touches, and the touched files that map to none."""
    by_file: dict[str, list] = {}
    for fn in index.functions.values():
        if fn.start_line is not None and fn.end_line is not None:
            by_file.setdefault(fn.file_path, []).append(fn)
    changed: set[str] = set()
    unmatched: list[str] = []
    for path, lines in sorted(patch_lines(diff).items()):
        if path not in index.files:
            unmatched.append(f"{path} (not in this build)")
            continue
        hit = {fn.qualified_name for fn in by_file.get(path, []) for n in lines if fn.start_line <= n <= fn.end_line}
        if hit:
            changed |= hit
        else:
            unmatched.append(f"{path} (module-level lines only)")
    return changed, unmatched


def _distances(start: set[str], adjacency: dict[str, set[str]], max_hops: int) -> dict[str, int]:
    """Hop distance from `start` along `adjacency` (callee -> callers), up to max_hops; `start` excluded."""
    seen = set(start)
    dist: dict[str, int] = {}
    frontier = set(start)
    for hop in range(1, max_hops + 1):
        nxt = {caller for node in frontier for caller in adjacency.get(node, ())} - seen
        for node in nxt:
            dist[node] = hop
        seen |= nxt
        frontier = nxt
    return dist


def analyze_impact(index: BuildIndex, diff: str | None = None, functions: list[str] | None = None,
                   max_hops: int = 3) -> ImpactReport:
    max_hops = max(1, min(int(max_hops), 6))
    report = ImpactReport(max_hops=max_hops)
    changed: set[str] = set()
    if diff:
        changed, report.unmatched = changed_from_diff(index, diff)
    for name in functions or []:
        qn = normalize_symbol(name, index, set())
        if qn in index.functions:
            changed.add(qn)
        elif qn in index.classes:  # a class change affects whoever constructs it
            init = index.constructor(qn)
            changed.update([init] if init else [])
        else:
            report.unmatched.append(f"{name} (no such function)")

    def info(qn: str, hops: int | None = None) -> dict:
        fn = index.functions[qn]
        row = {"qualified_name": qn, "file_path": fn.file_path,
               "start_line": fn.start_line, "end_line": fn.end_line}
        return row if hops is None else {**row, "hops": hops}

    report.changed = [info(qn) for qn in sorted(changed)]
    precise = _distances(changed, index.precise_in, max_hops)
    by_distance = sorted(precise.items(), key=lambda kv: (kv[1], kv[0]))
    report.tests = [{**info(qn, h), "pytest_id": index.functions[qn].pytest_id}
                    for qn, h in by_distance if index.functions[qn].is_test]
    report.affected = [info(qn, h) for qn, h in by_distance
                       if not index.functions[qn].is_test and index.functions[qn].file_path not in index.test_files]
    loose = _distances(changed, index.all_in, max_hops)
    report.possibly_affected = [info(qn, h) for qn, h in sorted(loose.items(), key=lambda kv: (kv[1], kv[0]))
                                if qn not in precise and not index.functions[qn].is_test]
    return report
