"""One-line LLM summaries for every non-test function (batched: several functions per call)."""

from __future__ import annotations

from collections.abc import Callable

from app.graph.store import GraphStore
from app.llm.client import LLMClient
from app.llm.prompts import render

MAX_CODE_CHARS = 2500


def summarize_functions(store: GraphStore, build_id: int, llm: LLMClient, model: str, batch_size: int = 8,
                        limit: int | None = None, progress: Callable[[str], None] | None = None) -> dict:
    rows = store.read(
        "MATCH (f:Function {build_id: $b}) WHERE f.in_test_file = false AND f.summary IS NULL "
        "RETURN f.qualified_name AS qn, f.code AS code ORDER BY f.qualified_name", {"b": build_id})
    if limit is not None:
        rows = rows[:limit]
    written = 0
    for i in range(0, len(rows), batch_size):
        if progress:
            progress(f"summarizing functions {i}/{len(rows)}")
        batch = rows[i:i + batch_size]
        ids = {f"f{j + 1}": r["qn"] for j, r in enumerate(batch)}
        listing = "\n\n".join(f"### {fid}: {r['qn']}\n```python\n{(r['code'] or '')[:MAX_CODE_CHARS]}\n```"
                              for fid, r in zip(ids, batch, strict=True))
        data, _ = llm.complete_json(model, [{"role": "user", "content": render("summarize", functions=listing)}],
                                    max_tokens=80 * len(batch) + 100, name="summarize")
        if not isinstance(data, dict):
            continue
        updates = [{"qualified_name": ids[fid], "props": {"summary": " ".join(str(text).split())[:300],
                                                           "summary_model": model}}
                   for fid, text in data.items() if fid in ids and text]
        store.set_function_props(build_id, updates)
        written += len(updates)
    return {"summarized_functions": written, "summary_model": model}
