"""Self-audit of the symbol checker: run it on the repository's own, correct code.

Every symbol the original functions use exists and every call they make is valid, so anything the checker flags
here is a false positive of the checker (mixins, metaprogramming, dynamic attributes...). The rate is an upper
bound on how often E4's "hallucination" verdicts blame a model for the checker's own blind spots; it is stored
with each build and shown next to the resolver statistics.
"""

from __future__ import annotations

from collections import Counter

from app.eval.index import BuildIndex
from app.eval.symbol_check import check_code
from app.graph.store import GraphStore


def symbol_self_audit(store: GraphStore, build_id: int, index: BuildIndex, max_examples: int = 10) -> dict:
    rows = store.read(
        "MATCH (f:Function {build_id: $b}) WHERE NOT f.in_test_file AND NOT f.is_nested "
        "RETURN f.qualified_name AS qn, f.module AS module, f.class_qn AS class_qn, f.code AS code "
        "ORDER BY qn", {"b": build_id})
    by_category: Counter[str] = Counter()
    examples: list[dict] = []
    flagged = checked = 0
    for r in rows:
        fn = index.functions.get(r["qn"])
        check = check_code(r["code"] or "", index, r["module"], r["class_qn"], fn.kind if fn else None)
        if not check.syntax_ok:  # code over the stored size limit is truncated; it cannot be judged
            continue
        checked += 1
        problems = check.problems
        if problems:
            flagged += 1
            by_category.update(u.category for u in problems)
            for u in problems[: max(0, max_examples - len(examples))]:
                examples.append({"function": r["qn"], "symbol": u.symbol, "category": u.category,
                                 "detail": u.detail, "line": u.line})
    return {"functions_checked": checked, "functions_flagged": flagged,
            "false_positive_rate": round(flagged / checked, 4) if checked else None,
            "by_category": dict(by_category), "examples": examples}
