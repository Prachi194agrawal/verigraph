"""Feature tags: the LLM proposes the repository's features, then tags functions with them.

IMPLEMENTS edges carry the model's confidence, capped below 1: they are the only edges in the graph that
come from an LLM, and they never feed gold answers.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from app.graph.store import GraphStore, uid
from app.llm.client import LLMClient
from app.llm.prompts import render

MAX_CONFIDENCE = 0.95
README_NAMES = ("README.md", "README.rst", "README.txt", "README")


def _readme(repo_root: Path | None) -> str:
    if repo_root is None:
        return ""
    for name in README_NAMES:
        p = repo_root / name
        if p.exists():
            return p.read_text("utf-8", errors="replace")[:3000]
    return ""


def tag_features(store: GraphStore, build_id: int, llm: LLMClient, model: str, repo_name: str,
                 repo_root: Path | None = None, batch_size: int = 10, limit: int | None = None,
                 progress: Callable[[str], None] | None = None) -> dict:
    say = progress or (lambda _m: None)
    say("features: proposing feature list")
    modules = store.read(
        "MATCH (f:Function {build_id: $b}) WHERE f.in_test_file = false "
        "WITH f.module AS module, collect(f.qualified_name + ': ' + coalesce(f.summary, f.signature)) AS fns "
        "RETURN module, fns[..4] AS samples ORDER BY module LIMIT 60", {"b": build_id})
    overview = _readme(repo_root) + "\n\nModules:\n" + "\n".join(
        f"- {m['module']}\n" + "\n".join(f"    {s}" for s in m["samples"]) for m in modules)
    data, _ = llm.complete_json(model, [{"role": "user", "content": render("features_propose", repo=repo_name,
                                                                           overview=overview)}],
                                max_tokens=1200, name="features_propose")
    features: dict[str, str] = {}
    for item in (data or {}).get("features", []) if isinstance(data, dict) else []:
        if isinstance(item, dict) and item.get("name"):
            features[" ".join(str(item["name"]).lower().split())[:60]] = str(item.get("description", ""))[:300]
    features = dict(list(features.items())[:12])

    store.write("MATCH (t:Feature {build_id: $b}) DETACH DELETE t", {"b": build_id})
    store.write("UNWIND $rows AS row CREATE (t:Feature) SET t = row",
                {"rows": [{"uid": uid(build_id, "feature", n), "build_id": build_id, "name": n, "description": d,
                           "model": model} for n, d in features.items()]})
    if not features:
        return {"features": 0, "implements_edges": 0}

    rows = store.read(
        "MATCH (f:Function {build_id: $b}) WHERE f.in_test_file = false "
        "RETURN f.qualified_name AS qn, f.signature AS signature, coalesce(f.summary, f.docstring, '') AS summary "
        "ORDER BY f.qualified_name", {"b": build_id})
    if limit is not None:
        rows = rows[:limit]
    feature_list = "\n".join(f"- {n}: {d}" for n, d in features.items())
    edges: list[dict] = []
    for i in range(0, len(rows), batch_size):
        say(f"features: tagging functions {i}/{len(rows)}")
        batch = rows[i:i + batch_size]
        ids = {f"f{j + 1}": r["qn"] for j, r in enumerate(batch)}
        listing = "\n".join(f"{fid}: {r['qn']}{r['signature'][len(r['qn'].rsplit('.', 1)[-1]):]} - {r['summary'][:200]}"
                            for fid, r in zip(ids, batch, strict=True))
        data, _ = llm.complete_json(model, [{"role": "user", "content": render(
            "features_tag", features=feature_list, functions=listing)}], max_tokens=60 * len(batch) + 100,
            name="features_tag")
        if not isinstance(data, dict):
            continue
        for fid, tags in data.items():
            if fid not in ids or not isinstance(tags, list):
                continue
            for tag in tags[:2]:
                if not isinstance(tag, dict):
                    continue
                name = " ".join(str(tag.get("feature", "")).lower().split())
                try:
                    conf = float(tag.get("confidence", 0.5))
                except (TypeError, ValueError):
                    conf = 0.5
                if name in features and conf > 0:
                    edges.append({"qn": ids[fid], "feature": uid(build_id, "feature", name),
                                  "confidence": round(min(conf, MAX_CONFIDENCE), 3)})
    store.write(
        "UNWIND $rows AS row MATCH (f:Function {build_id: $b, qualified_name: row.qn}) "
        "MATCH (t:Feature {uid: row.feature}) CREATE (f)-[:IMPLEMENTS {confidence: row.confidence, model: $model, "
        "source: 'llm'}]->(t)", {"b": build_id, "rows": edges, "model": model})
    return {"features": len(features), "implements_edges": len(edges), "feature_model": model}
