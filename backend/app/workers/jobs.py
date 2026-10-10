"""Job bodies. Each takes a row id, runs its step(s), and records status, progress and errors on the row,
so the API and dashboard can follow along. They run in a Celery worker, a thread of the API, or the CLI.

  run_build       steps 1-3: checkout, structure, history, meaning (+ embeddings) -> Neo4j
  run_dataset     step 4:    questions with Cypher gold answers -> questions table
  run_experiment  steps 5-6: one model x one pipeline over a dataset, every answer scored
"""

from __future__ import annotations

import logging
import time
import traceback
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from sqlalchemy import delete, select

from app import PARSER_VERSION
from app.analysis.symbol_audit import symbol_self_audit
from app.config import get_settings
from app.db import session_scope
from app.eval.index import load_build_index
from app.eval.report import answer_rows, summarize
from app.eval.scorer import QuestionView, ScoredAnswer, Scorer
from app.graph.builder import build_structure
from app.graph.store import get_store
from app.ingest.clone import checkout_repo
from app.ingest.github import enrich_from_github
from app.ingest.history import mine_history
from app.llm.client import get_llm
from app.models import (
    Answer,
    ClaimVerdict,
    Dataset,
    Experiment,
    GraphBuild,
    Question,
    Score,
    SymbolCheck,
    TestRun,
    utcnow,
)
from app.pipelines import make_pipeline
from app.pipelines.base import PipelineContext, PipelineOutput, QuestionInput
from app.semantic.embeddings import embed_functions, get_embedder
from app.semantic.features import tag_features
from app.semantic.summaries import summarize_functions
from app.testgen.generator import generate_questions
from app.testgen.templates import FAMILIES

log = logging.getLogger(__name__)


def _progress(model, row_id: int) -> Callable[[str], None]:
    last = [0.0]

    def say(message: str) -> None:
        log.info("[%s %s] %s", model.__tablename__, row_id, message)
        now = time.monotonic()
        if now - last[0] < 1.0:  # throttle database writes
            return
        last[0] = now
        with session_scope() as db:
            db.get(model, row_id).progress = message

    return say


def _fail(model, row_id: int) -> None:
    error = traceback.format_exc()
    log.error("%s %s failed:\n%s", model.__tablename__, row_id, error)
    try:
        with session_scope() as db:
            row = db.get(model, row_id)
            row.status, row.error, row.finished_at = "failed", error[-8000:], utcnow()
    except Exception:  # noqa: BLE001 - the database itself may be what failed
        log.exception("could not record the failure of %s %s", model.__tablename__, row_id)


# -- steps 1-3 ---------------------------------------------------------------------------------------------

def run_build(build_id: int) -> None:
    settings = get_settings()
    say = _progress(GraphBuild, build_id)
    try:
        with session_scope() as db:
            build = db.get(GraphBuild, build_id)
            url, name, graph_id = build.repository.url, build.repository.name, build.graph_id
            opts = dict(build.options or {})
            requested = opts.get("commit_sha") or build.repository.commit_sha
            build.status, build.error, build.parser_version = "running", None, PARSER_VERSION
        say("step 1: checking out the repository")
        checkout = checkout_repo(url, requested, settings.data_dir / "builds" / str(build_id) / "repo",
                                 settings.data_dir / "mirrors")
        say("step 2: parsing and resolving calls")
        graph = build_structure(checkout.path, name, url, checkout.commit_sha,
                                opts.get("resolvers") or settings.resolver_order, settings.scip_python_bin, say)
        stats = dict(graph.stats)
        if opts.get("history", True) and checkout.has_history:
            say("step 3: mining git history")
            stats["history"] = mine_history(checkout.path, graph, int(opts.get("max_commits")
                                                                      or settings.max_history_commits), say)
            if opts.get("github", True) and checkout.github_slug:
                stats["github"] = enrich_from_github(graph, checkout.github_slug, settings.github_token,
                                                     settings.max_pull_requests,
                                                     settings.github_unauthenticated_budget, say)
        store = get_store()
        store.write_graph(graph_id, graph, say)
        if opts.get("semantic_model"):
            say("step 3: LLM summaries and feature tags")
            llm, model, limit = get_llm(), opts["semantic_model"], opts.get("semantic_limit")
            stats["summaries"] = summarize_functions(store, graph_id, llm, model, limit=limit, progress=say)
            stats["features"] = tag_features(store, graph_id, llm, model, name, checkout.path, limit=limit,
                                             progress=say)
        if opts.get("embeddings", True):
            say("step 3: code embeddings")
            embedder = get_embedder(opts.get("embedding_model") or settings.embedding_model)
            stats["embeddings"] = embed_functions(store, graph_id, embedder, say)
        stats["counts"] = store.counts(graph_id)
        index = load_build_index(store, graph_id, refresh=True)
        say("checking the symbol checker on the repository's own code")
        try:
            stats["symbol_audit"] = symbol_self_audit(store, graph_id, index)
        except Exception as exc:  # noqa: BLE001 - a diagnostic must never fail the build
            log.warning("symbol self-audit of build %s failed: %s", build_id, exc)
            stats["symbol_audit"] = {"error": str(exc)[:500]}
        with session_scope() as db:
            build = db.get(GraphBuild, build_id)
            build.status, build.progress, build.stats = "done", "done", stats
            build.commit_sha, build.checkout_path = checkout.commit_sha, str(checkout.path)
            build.unresolved_call_rate, build.finished_at = stats.get("unresolved_call_rate"), utcnow()
    except Exception:  # noqa: BLE001 - recorded on the row
        _fail(GraphBuild, build_id)


# -- step 4 ------------------------------------------------------------------------------------------------

def run_dataset(dataset_id: int) -> None:
    say = _progress(Dataset, dataset_id)
    try:
        with session_scope() as db:
            ds = db.get(Dataset, dataset_id)
            graph_id, version, opts = db.get(GraphBuild, ds.build_id).graph_id, ds.version, dict(ds.options or {})
            ds.status, ds.error = "running", None
        drafts = generate_questions(
            get_store(), graph_id, families=opts.get("families") or list(FAMILIES),
            per_family=int(opts.get("per_family", 20)), seed=int(opts.get("seed", 0)), llm=get_llm(),
            rephrase_model=opts.get("rephrase_model"), progress=say)
        with session_scope() as db:
            # Celery redelivers a job whose worker died (acks_late): replace questions from an earlier attempt.
            db.execute(delete(Question).where(Question.dataset_id == dataset_id))
            for d in drafts:
                db.add(Question(
                    dataset_id=dataset_id, dataset_version=version, family=d.family, text=d.text,
                    template_text=d.meta.pop("template_text", d.text), gold_cypher=d.gold_cypher,
                    gold_params=d.gold_params, gold_answer=d.gold_answer, hop_count=d.hop_count, target=d.target,
                    meta=d.meta))
            ds = db.get(Dataset, dataset_id)
            ds.n_questions, ds.status, ds.progress, ds.finished_at = len(drafts), "done", "done", utcnow()
    except Exception:  # noqa: BLE001
        _fail(Dataset, dataset_id)


# -- steps 5-6 ---------------------------------------------------------------------------------------------

def persist_answer(db, experiment_id: int, question_id: int, out: PipelineOutput, scored: ScoredAnswer) -> Answer:
    answer = Answer(
        experiment_id=experiment_id, question_id=question_id, text=out.raw_text, parsed_items=scored.items,
        match={"raw": out.items, "tp": scored.tp, "fp": scored.fp, "fn": scored.fn, "invented": scored.invented},
        explanation=out.explanation,
        code=out.code, retrieved_nodes=out.retrieved_nodes[:300], steps=out.steps, latency_ms=out.latency_ms,
        prompt_tokens=out.prompt_tokens, completion_tokens=out.completion_tokens,
        tokens=out.prompt_tokens + out.completion_tokens, cost_usd=out.cost_usd, error=out.error,
        trace_id=out.trace_id)
    db.add(answer)
    db.flush()
    for v in scored.verdicts:
        db.add(ClaimVerdict(answer_id=answer.id, subject=v.claim.subject[:500], relation=v.claim.relation,
                            object=v.claim.object[:500], verdict=v.label, evidence_edge=v.evidence,
                            extractor=v.claim.extractor, claim_text=v.claim.text[:2000]))
    for u in scored.symbols:
        db.add(SymbolCheck(answer_id=answer.id, symbol=u.symbol[:500], kind=u.kind, exists=u.exists,
                           arity_ok=u.arity_ok, category=u.category, detail=u.detail, line=u.line))
    if scored.test_run is not None:
        t = scored.test_run
        db.add(TestRun(answer_id=answer.id, tests_run=t.tests_run, passed=t.passed, failed=t.failed, status=t.status,
                       log_path=t.log_path))
    for metric, value in scored.scores.items():
        db.add(Score(answer_id=answer.id, metric=metric, value=float(value)))
    return answer


def run_experiment(experiment_id: int) -> None:
    try:
        _run_experiment(experiment_id)
    except Exception:  # noqa: BLE001 - recorded on the row
        _fail(Experiment, experiment_id)


def _run_experiment(experiment_id: int) -> None:
    with session_scope() as db:
        exp = db.get(Experiment, experiment_id)
        ds = db.get(Dataset, exp.dataset_id)
        build = db.get(GraphBuild, ds.build_id)
        opts = dict(exp.options or {})
        model, pipeline_name, prompt_version, judge = exp.model, exp.pipeline, exp.prompt_version, exp.judge_model
        build_id, graph_id, repo_name, commit_sha = build.id, build.graph_id, build.repository.name, build.commit_sha
        checkout = Path(build.checkout_path) if build.checkout_path else None
        embedding_model = (build.stats or {}).get("embeddings", {}).get("embedding_model")
        query = select(Question).where(Question.dataset_id == ds.id).order_by(Question.id)
        if opts.get("families"):
            query = query.where(Question.family.in_(opts["families"]))
        questions = list(db.execute(query).scalars())
        if opts.get("limit"):
            questions = questions[: int(opts["limit"])]
        if pipeline_name == "cypher_agent":  # it could read the original function out of the graph
            questions = [q for q in questions if q.family != "codegen"]
        done = set(db.execute(select(Answer.question_id).where(Answer.experiment_id == experiment_id)).scalars())
        todo = [(q.id, q.family, q.text, dict(q.meta or {}), list(q.gold_answer or []), q.hop_count)
                for q in questions if q.id not in done]
        exp.status, exp.error = "running", None
    say = _progress(Experiment, experiment_id)
    if pipeline_name == "vector" and not embedding_model:
        raise RuntimeError("the vector pipeline needs a build with code embeddings; rebuild with embeddings on")
    store, llm = get_store(), get_llm()
    index = load_build_index(store, graph_id)
    ctx = PipelineContext(store, llm, index, graph_id, repo_name, commit_sha, prompt_version,
                          embedding_model=embedding_model, options=opts)
    pipeline = make_pipeline(pipeline_name, ctx)
    scorer = Scorer(index, llm, judge, run_tests=bool(opts.get("run_tests")), checkout=checkout,
                    build_id=build_id)

    def work(item):
        qid, family, text, meta, gold, hops = item
        out = pipeline.answer(QuestionInput(qid, family, text, meta), model)
        scored = scorer.score(QuestionView(qid, family, text, gold, hops, meta), out,
                              answer_key=f"e{experiment_id}-q{qid}")
        for metric, value in scored.scores.items():
            llm.tracer.score(out.trace_id, metric, value)
        return qid, out, scored

    workers = max(1, int(opts.get("concurrency", 4)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(work, item) for item in todo]
        try:
            for i, future in enumerate(as_completed(futures), 1):
                qid, out, scored = future.result()
                with session_scope() as db:  # only this thread writes to the database
                    persist_answer(db, experiment_id, qid, out, scored)
                say(f"answered {i}/{len(todo)} ({len(done)} done earlier)")
        except BaseException:
            for f in futures:  # don't keep paying for answers that can no longer be stored
                f.cancel()
            raise
    llm.tracer.flush()
    with session_scope() as db:
        exp = db.get(Experiment, experiment_id)
        exp.summary = summarize(answer_rows(db, experiment_id))
        exp.status, exp.progress, exp.finished_at = "done", "done", utcnow()


JOBS: dict[str, Callable[[int], None]] = {"build": run_build, "dataset": run_dataset, "experiment": run_experiment}
