"""Graph builds: status, quality report, and read access for the explorer (symbols, neighborhoods, files)."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from neo4j.exceptions import DriverError, Neo4jError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analysis.ask import BuildRef, ask
from app.analysis.impact import analyze_impact
from app.cleanup import active_jobs, delete_build
from app.config import get_settings
from app.db import get_db
from app.eval.index import load_build_index
from app.eval.meta_eval import edge_precision, sample_edges
from app.graph.store import get_store
from app.llm.client import get_llm
from app.models import EdgeAudit, GraphBuild, SemanticAudit
from app.pipelines import PIPELINES
from app.schemas import AskRequest, AuditLabel, BuildOut, ImpactRequest

router = APIRouter(tags=["builds"])
MAX_FILE_BYTES = 2_000_000


def _build(db: Session, build_id: int) -> GraphBuild:
    build = db.get(GraphBuild, build_id)
    if build is None:
        raise HTTPException(404, "build not found")
    return build


@router.get("/builds", response_model=list[BuildOut])
def list_builds(db: Session = Depends(get_db)) -> list[GraphBuild]:
    return list(db.execute(select(GraphBuild).order_by(GraphBuild.id.desc())).scalars())


@router.get("/builds/{build_id}", response_model=BuildOut)
def get_build(build_id: int, db: Session = Depends(get_db)) -> GraphBuild:
    return _build(db, build_id)


@router.get("/builds/{build_id}/quality")
def build_quality(build_id: int, db: Session = Depends(get_db)) -> dict:
    """Graph stats, unresolved-call rate, resolver breakdown, and the results of hand audits."""
    build = _build(db, build_id)
    stats = build.stats or {}
    audits = [{"resolved_by": a.resolved_by, "label": a.label}
              for a in db.execute(select(EdgeAudit).where(EdgeAudit.build_id == build_id)).scalars()]
    semantic = list(db.execute(select(SemanticAudit).where(SemanticAudit.build_id == build_id)).scalars())
    return {
        "build_id": build_id, "graph_id": build.graph_id, "status": build.status, "commit_sha": build.commit_sha,
        "parser_version": build.parser_version,
        "unresolved_call_rate": build.unresolved_call_rate, "name_only_rate": stats.get("name_only_rate"),
        "call_sites": stats.get("call_sites"), "calls_by_resolver": stats.get("by_resolver"),
        "calls_edges_by_resolver": stats.get("calls_edges_by_resolver"), "external_calls": stats.get("external"),
        "counts": stats.get("counts"), "history": stats.get("history"), "github": stats.get("github"),
        "embeddings": stats.get("embeddings"), "summaries": stats.get("summaries"), "features": stats.get("features"),
        "symbol_audit": stats.get("symbol_audit"),
        "edge_audit": edge_precision(audits) if audits else {},
        "semantic_audit": {
            "judged": sum(a.judge_label is not None for a in semantic),
            "judge_faithful_rate": (sum(a.judge_label == "faithful" for a in semantic)
                                    / max(1, sum(a.judge_label is not None for a in semantic))) if semantic else None,
            "human_labeled": sum(a.human_label is not None for a in semantic),
        },
    }


@router.get("/builds/{build_id}/symbols")
def search_symbols(build_id: int, q: str = Query(min_length=1), limit: int = Query(20, ge=1, le=100),
                   db: Session = Depends(get_db)) -> list[dict]:
    return get_store().search_symbols(_build(db, build_id).graph_id, q, limit)


@router.get("/builds/{build_id}/node")
def node_details(build_id: int, qn: str, db: Session = Depends(get_db)) -> dict:
    node = get_store().node_details(_build(db, build_id).graph_id, qn)
    if node is None:
        raise HTTPException(404, "symbol not found")
    return node


@router.get("/builds/{build_id}/neighborhood")
def neighborhood(build_id: int, qn: str, depth: int = Query(1, ge=1, le=3), db: Session = Depends(get_db)) -> dict:
    return get_store().neighborhood(_build(db, build_id).graph_id, qn, depth)


@router.get("/builds/{build_id}/file")
def file_content(build_id: int, path: str, db: Session = Depends(get_db)) -> dict:
    build = _build(db, build_id)
    if not build.checkout_path:
        raise HTTPException(409, "build has no checkout")
    root = Path(build.checkout_path).resolve()
    target = (root / path).resolve()
    if root not in target.parents or not target.is_file():
        raise HTTPException(404, "file not found in this build")
    if target.stat().st_size > MAX_FILE_BYTES:
        raise HTTPException(413, "file too large")
    functions = get_store().read(
        "MATCH (f:Function {build_id: $b}) WHERE f.file_path = $p RETURN f.qualified_name AS qualified_name, "
        "f.start_line AS start_line, f.end_line AS end_line ORDER BY start_line", {"b": build.graph_id, "p": path})
    return {"path": path, "content": target.read_text("utf-8", errors="replace"), "functions": functions}


@router.post("/builds/{build_id}/audit")
def create_edge_audit(build_id: int, per_resolver: int = 100, seed: int = 0, db: Session = Depends(get_db)) -> dict:
    """Sample CALLS edges per resolver for hand-checking (step "trusting the graph")."""
    build = _build(db, build_id)
    existing = db.execute(select(EdgeAudit.id).where(EdgeAudit.build_id == build_id)).first()
    if existing:
        raise HTTPException(409, "this build already has an audit sample")
    rows = sample_edges(get_store(), build.graph_id, per_resolver, seed)
    for r in rows:
        db.add(EdgeAudit(build_id=build_id, caller=r["caller"], callee=r["callee"], resolved_by=r["resolved_by"],
                         file_path=r["file_path"], line=r["line"]))
    return {"sampled": len(rows)}


@router.get("/builds/{build_id}/audit")
def list_edge_audit(build_id: int, db: Session = Depends(get_db)) -> list[dict]:
    rows = db.execute(select(EdgeAudit).where(EdgeAudit.build_id == build_id).order_by(EdgeAudit.id)).scalars()
    return [{"id": a.id, "caller": a.caller, "callee": a.callee, "resolved_by": a.resolved_by,
             "file_path": a.file_path, "line": a.line, "label": a.label, "notes": a.notes} for a in rows]


@router.put("/audits/{audit_id}")
def label_edge(audit_id: int, body: AuditLabel, db: Session = Depends(get_db)) -> dict:
    audit = db.get(EdgeAudit, audit_id)
    if audit is None:
        raise HTTPException(404, "audit row not found")
    audit.label, audit.notes = body.label, body.notes
    return {"id": audit.id, "label": audit.label}


def _finished(db: Session, build_id: int) -> GraphBuild:
    build = _build(db, build_id)
    if build.status != "done":
        raise HTTPException(409, "build is not finished")
    return build


@router.post("/builds/{build_id}/ask")
def ask_build(build_id: int, body: AskRequest, db: Session = Depends(get_db)) -> dict:
    """Answer a free-form question with one pipeline and check the answer's claims against the graph."""
    build = _finished(db, build_id)
    if body.pipeline not in PIPELINES:
        raise HTTPException(422, f"unknown pipeline {body.pipeline!r}; choose from {sorted(PIPELINES)}")
    embedding_model = (build.stats or {}).get("embeddings", {}).get("embedding_model")
    if body.pipeline == "vector" and not embedding_model:
        raise HTTPException(409, "vector pipeline requires a build with code embeddings enabled")
    store = get_store()
    ref = BuildRef(build.id, build.graph_id, build.repository.name, build.commit_sha, embedding_model)
    return ask(store, get_llm(), load_build_index(store, build.graph_id), ref, body.question.strip(), body.model.strip(),
               body.pipeline, body.prompt_version, body.judge_model, body.options)


@router.post("/builds/{build_id}/impact")
def change_impact(build_id: int, body: ImpactRequest, db: Session = Depends(get_db)) -> dict:
    """Functions and tests a change can affect, from the precise call graph (no LLM). Diff line numbers are
    read on the old side, so the build should be at the commit the diff applies to."""
    build = _finished(db, build_id)
    if not body.diff and not body.functions:
        raise HTTPException(422, "give a unified diff or at least one function name")
    report = analyze_impact(load_build_index(get_store(), build.graph_id), body.diff, body.functions, body.max_hops)
    title = f"VeriGraph change impact ({build.repo} @ {(build.commit_sha or 'working tree')[:10]})"
    return {**report.to_dict(), "markdown": report.to_markdown(title, build.unresolved_call_rate)}


@router.delete("/builds/{build_id}")
def remove_build(build_id: int, db: Session = Depends(get_db)) -> dict:
    """Delete the build's graph, checkout and results (datasets, experiments, answers, labels)."""
    _build(db, build_id)
    running = active_jobs(db, build_id)
    if running:
        raise HTTPException(409, f"wait for these jobs to finish first: {', '.join(running)}")
    try:
        return delete_build(db, get_store(), build_id, get_settings().data_dir)
    except (Neo4jError, DriverError) as exc:  # Neo4j goes first: nothing was deleted yet
        raise HTTPException(503, f"could not delete the graph from Neo4j: {exc}") from exc
