"""Ask the graph a free-form question and check extracted claims against recorded graph facts.

This is the developer-facing use of the knowledge graph ("which tests cover `Session.send`?"). It runs the same
pipelines as the experiments, but a question has no gold answer, so the graph grades what it can: each answer
item is marked as an entity recognized by the graph or not, and each extracted relationship the explanation
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
        pipeline: str = "graphrag", prompt_version: str = "v2", judge_model: str | None = None,
        options: dict | None = None) -> dict:
    ctx = PipelineContext(store, llm, index, build.graph_id, build.repo_name, build.commit_sha, prompt_version,
                          embedding_model=build.embedding_model, options=dict(options or {}))
    out = make_pipeline(pipeline, ctx).answer(QuestionInput(None, "ask", question, {}), model)
    judge_error = None
    try:
        verdicts = check_explanation(out.explanation, index, llm, judge_model) if out.explanation and not out.error \
            else []
    except Exception as exc:  # noqa: BLE001 - retain the answer and the deterministic rule checks
        verdicts = check_explanation(out.explanation, index)
        judge_error = f"{type(exc).__name__}: {str(exc)[:300]}"
    items = [_item(i, index) for i in dict.fromkeys(out.items)]
    labels = [v.label for v in verdicts]
    limitations = ["The graph contains static observations at the analyzed commit; dynamic dispatch, "
                   "framework behavior and cross-language interfaces may be missing.",
                   "An item absent from the graph is unrecognized, not proof that it is invented.",
                   "Only extracted claims are checked; no reference answer exists for this free-form question."]
    if model.startswith("mock/"):
        limitations.append("This mock model is a test fixture; its answers do not represent a real LLM.")
    if build.embedding_model == "hash":
        limitations.append("Hash embeddings retrieve lexical similarity, not neural semantic similarity.")
    if not index.functions:
        limitations.append("This build contains file inventory without function relationships; "
                            "source behavior and call-chain questions cannot be verified from this graph.")
    if judge_error:
        limitations.append(f"The optional claim extractor failed; only rule-extracted claims were checked. {judge_error}")
    return {
        "question": question, "model": model, "pipeline": pipeline, "build_id": build.build_id,
        "judge_error": judge_error,
        "answer": items, "explanation": out.explanation, "raw_text": out.raw_text, "error": out.error,
        "claims": [{"subject": v.claim.subject, "relation": v.claim.relation, "object": v.claim.object,
                    "verdict": v.label, "evidence": v.evidence, "extractor": v.claim.extractor,
                    "claim_text": v.claim.text} for v in verdicts],
        "summary": {"claims": len(labels), "supported": labels.count("supported"),
                    "contradicted": labels.count("contradicted"), "unverifiable": labels.count("unverifiable"),
                    "unrecognized_items": sum(i["in_graph"] is False for i in items),
                    "invented_items": sum(i["in_graph"] is False for i in items)},  # legacy field: not proof of invention
        "context": out.context, "retrieved_nodes": out.retrieved_nodes[:300], "steps": out.steps,
        "latency_ms": round(out.latency_ms, 1), "prompt_tokens": out.prompt_tokens,
        "completion_tokens": out.completion_tokens, "cost_usd": out.cost_usd, "trace_id": out.trace_id,
        "provenance": {"commit_sha": build.commit_sha, "reference_source": "static_code_graph",
                       "model_kind": "mock" if model.startswith("mock/") else "llm",
                       "embedding_model": build.embedding_model,
                       "languages": sorted(set(index.file_languages.values()))},
        "limitations": limitations,
    }
