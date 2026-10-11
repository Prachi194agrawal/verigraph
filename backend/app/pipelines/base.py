"""Shared pipeline machinery: retrieve -> prompt -> one LLM call -> parse. The Cypher agent overrides answer()."""

from __future__ import annotations

import time
import traceback
from dataclasses import dataclass, field

from app.eval.index import BuildIndex, codegen_language
from app.graph.store import GraphStore
from app.llm.client import LLMClient
from app.llm.prompts import render
from app.pipelines.parsing import parse_output

QA_MAX_TOKENS = 800
CODE_MAX_TOKENS = 1500


@dataclass
class QuestionInput:
    """What a pipeline is allowed to see: never the gold answer."""

    id: int | None
    family: str
    text: str
    meta: dict = field(default_factory=dict)


@dataclass
class PipelineContext:
    store: GraphStore
    llm: LLMClient
    index: BuildIndex
    build_id: int
    repo_name: str
    commit_sha: str | None
    prompt_version: str = "v2"
    embedding_model: str | None = None
    options: dict = field(default_factory=dict)


@dataclass
class Retrieval:
    context: str = ""
    nodes: list[str] = field(default_factory=list)  # qualified names, paths, "#PR", authors shown to the model


@dataclass
class PipelineOutput:
    raw_text: str = ""
    items: list[str] = field(default_factory=list)
    explanation: str = ""
    code: str | None = None
    context: str = ""
    retrieved_nodes: list[str] = field(default_factory=list)
    steps: list[dict] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    trace_id: str = ""
    error: str | None = None


class Pipeline:
    name = ""

    def __init__(self, ctx: PipelineContext) -> None:
        self.ctx = ctx

    def retrieve(self, q: QuestionInput) -> Retrieval:
        return Retrieval()

    def system_prompt(self, q: QuestionInput) -> str:
        template = "codegen_system" if q.family == "codegen" else "answer_system"
        return render(template, self.ctx.prompt_version, repo=self.ctx.repo_name,
                      commit=(self.ctx.commit_sha or "working tree")[:12])

    def messages(self, q: QuestionInput, retrieval: Retrieval) -> list[dict]:
        user = f"Question: {q.text}"
        if retrieval.context:
            user = f"Context from the repository:\n\n{retrieval.context}\n\n{user}"
        return [{"role": "system", "content": self.system_prompt(q)}, {"role": "user", "content": user}]

    def answer(self, q: QuestionInput, model: str) -> PipelineOutput:
        tracer = self.ctx.llm.tracer
        out = PipelineOutput(trace_id=tracer.new_trace_id())
        if q.family == "codegen":
            language = codegen_language(self.ctx.index, q.meta)
            if language != "python":
                out.error = f"Code-generation checks currently support Python; {language} tasks are not supported."
                return out
        started = time.perf_counter()
        try:
            retrieval = self.retrieve(q)
            retrieval_ms = (time.perf_counter() - started) * 1000
            out.context, out.retrieved_nodes = retrieval.context, retrieval.nodes
            tracer.log(out.trace_id, "retrieval", {
                "input": q.text, "output": retrieval.context,
                "metadata": {"pipeline": self.name, "family": q.family, "question_id": q.id, "nodes": retrieval.nodes}})
            resp = self.ctx.llm.complete(
                model, self.messages(q, retrieval), json_mode=q.family != "codegen",
                max_tokens=CODE_MAX_TOKENS if q.family == "codegen" else QA_MAX_TOKENS,
                trace_id=out.trace_id, name=f"{self.name}:{q.family}")
            out.raw_text = resp.text
            out.items, out.explanation, out.code = parse_output(resp.text, q.family)
            out.prompt_tokens, out.completion_tokens = resp.prompt_tokens, resp.completion_tokens
            out.cost_usd = resp.cost_usd
            out.latency_ms = retrieval_ms + resp.latency_ms  # the original latency, even on a cache hit
        except Exception as exc:  # noqa: BLE001 - one failed question must not stop an experiment
            out.error = f"{type(exc).__name__}: {exc}"[:2000]
            out.latency_ms = (time.perf_counter() - started) * 1000
            tracer.log(out.trace_id, "error", {"output": traceback.format_exc()[-4000:]})
        return out
