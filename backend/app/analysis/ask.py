"""Ask the graph a free-form question with any pipeline, and check every claim of the answer against the graph.

This is the developer-facing use of the knowledge graph ("which tests cover `Session.send`?"). It runs the same
pipelines as the experiments, but a question has no gold answer, so the graph grades what it can: each answer
item is marked as an entity of the repository or not (an invented name), and each relationship the explanation
states is labelled supported / contradicted / unverifiable with its evidence edge.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.eval.claim_check import check_explanation
from app.eval.index import BuildIndex
from app.eval.normalize import normalize_path, normalize_symbol
from app.graph.store import GraphStore
from app.llm.client import LLMClient
from app.pipelines import make_pipeline
from app.pipelines.base import PipelineContext, QuestionInput


@dataclass
class BuildRef:
    build_id: int  # row id, reported back to the caller
    graph_id: int  # Neo4j key of the build's nodes, used for retrieval
    repo_name: str
    commit_sha: str | None
    embedding_model: str | None


def _item(raw: str, index: BuildIndex) -> dict:
    qn = normalize_symbol(raw, index, set())
    if index.is_entity(qn):
        return {"raw": raw, "name": qn, "in_graph": True, "kind": "function" if qn in index.functions else "class"}
    path = normalize_path(raw, index, set())
    if path in index.files:
        return {"raw": raw, "name": path, "in_graph": True, "kind": "file"}
    looks_like_code = "." in raw or "_" in raw or raw.endswith(")")
    return {"raw": raw, "name": raw, "in_graph": False if looks_like_code else None, "kind": None}


def ask(store: GraphStore, llm: LLMClient, index: BuildIndex, build: BuildRef, question: str, model: str,
        pipeline: str = "graphrag", prompt_version: str = "v1", judge_model: str | None = None,
        options: dict | None = None) -> dict:
    ctx = PipelineContext(store, llm, index, build.graph_id, build.repo_name, build.commit_sha, prompt_version,
                          embedding_model=build.embedding_model, options=dict(options or {}))
    out = make_pipeline(pipeline, ctx).answer(QuestionInput(None, "ask", question, {}), model)
    verdicts = check_explanation(out.explanation, index, llm, judge_model) if out.explanation and not out.error \
        else []
    items = [_item(i, index) for i in dict.fromkeys(out.items)]
    labels = [v.label for v in verdicts]
    return {
        "question": question, "model": model, "pipeline": pipeline, "build_id": build.build_id,
        "answer": items, "explanation": out.explanation, "raw_text": out.raw_text, "error": out.error,
        "claims": [{"subject": v.claim.subject, "relation": v.claim.relation, "object": v.claim.object,
                    "verdict": v.label, "evidence": v.evidence, "extractor": v.claim.extractor,
                    "claim_text": v.claim.text} for v in verdicts],
        "summary": {"claims": len(labels), "supported": labels.count("supported"),
                    "contradicted": labels.count("contradicted"), "unverifiable": labels.count("unverifiable"),
                    "invented_items": sum(i["in_graph"] is False for i in items)},
        "context": out.context, "retrieved_nodes": out.retrieved_nodes[:300], "steps": out.steps,
        "latency_ms": round(out.latency_ms, 1), "prompt_tokens": out.prompt_tokens,
        "completion_tokens": out.completion_tokens, "cost_usd": out.cost_usd, "trace_id": out.trace_id,
    }
