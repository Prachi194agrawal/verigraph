"""POST /experiments: run models x pipelines on a dataset (one experiment row per pair)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.eval.report import answer_rows, summarize
from app.models import Answer, Dataset, Experiment, GraphBuild
from app.pipelines import PIPELINES
from app.schemas import ExperimentCreate, ExperimentOut
from app.workers.dispatch import enqueue

router = APIRouter(prefix="/experiments", tags=["experiments"])


@router.post("", response_model=list[ExperimentOut], status_code=202)
def create_experiments(body: ExperimentCreate, background: BackgroundTasks,
                       db: Session = Depends(get_db)) -> list[Experiment]:
    ds = db.get(Dataset, body.dataset_id)
    if ds is None or ds.status != "done":
        raise HTTPException(409, "dataset not found or not finished")
    if not ds.n_questions:
        raise HTTPException(409, "This dataset has no questions. Explore the graph or generate a dataset with supported symbols first.")
    unknown = set(body.pipelines) - PIPELINES.keys()
    if unknown:
        raise HTTPException(422, f"unknown pipelines: {sorted(unknown)}")
    if "vector" in body.pipelines:
        build = db.get(GraphBuild, ds.build_id)
        if not build or not (build.stats or {}).get("embeddings", {}).get("embedding_model"):
            raise HTTPException(409, "vector pipeline requires a build with code embeddings enabled")
    group = uuid.uuid4().hex[:12]
    options = {**body.options, "limit": body.limit, "families": body.families, "concurrency": body.concurrency,
               "run_tests": body.run_tests}
    created = []
    for model in body.models:
        for pipeline in body.pipelines:
            exp = Experiment(dataset_id=ds.id, model=model, pipeline=pipeline, prompt_version=body.prompt_version,
                             judge_model=body.judge_model, run_group=group,
                             options={k: v for k, v in options.items() if v is not None})
            db.add(exp)
            created.append(exp)
    db.flush()
    db.commit()
    for exp in created:
        enqueue("experiment", exp.id, background)
    return created


@router.get("", response_model=list[ExperimentOut])
def list_experiments(dataset_id: int | None = None, db: Session = Depends(get_db)) -> list[Experiment]:
    query = select(Experiment).order_by(Experiment.id.desc())
    if dataset_id is not None:
        query = query.where(Experiment.dataset_id == dataset_id)
    return list(db.execute(query).scalars())


@router.get("/{experiment_id}", response_model=ExperimentOut)
def get_experiment(experiment_id: int, db: Session = Depends(get_db)) -> Experiment:
    exp = db.get(Experiment, experiment_id)
    if exp is None:
        raise HTTPException(404, "experiment not found")
    return exp


@router.post("/{experiment_id}/resume", response_model=ExperimentOut, status_code=202)
def resume_experiment(experiment_id: int, background: BackgroundTasks, db: Session = Depends(get_db)) -> Experiment:
    """Re-enqueue a failed experiment, or a finished one with failed model calls: answered questions are kept and
    failed calls (rate limits, timeouts) are asked again."""
    exp = db.get(Experiment, experiment_id)
    if exp is None:
        raise HTTPException(404, "experiment not found")
    failed_calls = db.scalar(select(func.count()).select_from(Answer).where(
        Answer.experiment_id == experiment_id, Answer.error.is_not(None)))
    if exp.status != "failed" and not (exp.status == "done" and failed_calls):
        raise HTTPException(409, "only failed experiments, or finished ones with failed model calls, can be resumed; "
                                 "an active job may still be running")
    exp.status, exp.error = "queued", None
    db.commit()
    enqueue("experiment", exp.id, background)
    return exp


@router.get("/{experiment_id}/results")
def experiment_results(experiment_id: int, db: Session = Depends(get_db)) -> dict:
    """Summary metrics plus per-question scores."""
    exp = db.get(Experiment, experiment_id)
    if exp is None:
        raise HTTPException(404, "experiment not found")
    rows = answer_rows(db, experiment_id)
    return {"experiment": ExperimentOut.model_validate(exp).model_dump(),
            "summary": exp.summary or summarize(rows), "answers": rows}
