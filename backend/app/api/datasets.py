"""POST /datasets: generate questions from a build; read and export them."""

from __future__ import annotations

import json

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Dataset, GraphBuild, Question
from app.schemas import DatasetCreate, DatasetOut, QuestionOut
from app.testgen.templates import FAMILIES
from app.workers.dispatch import enqueue

router = APIRouter(prefix="/datasets", tags=["datasets"])


def question_record(q: Question) -> dict:
    return {"family": q.family, "text": q.text, "template_text": q.template_text, "gold_answer": q.gold_answer,
            "gold_cypher": q.gold_cypher, "gold_params": q.gold_params, "hop_count": q.hop_count, "target": q.target,
            "meta": q.meta}


@router.post("", response_model=DatasetOut, status_code=202)
def create_dataset(body: DatasetCreate, background: BackgroundTasks, db: Session = Depends(get_db)) -> Dataset:
    build = db.get(GraphBuild, body.build_id)
    if build is None or build.status != "done":
        raise HTTPException(409, "build not found or not finished")
    unknown = set(body.families or []) - FAMILIES.keys()
    if unknown:
        raise HTTPException(422, f"unknown families: {sorted(unknown)}")
    version = body.version or f"{build.repo.replace('/', '-')}-b{build.id}-s{body.seed}"
    ds = Dataset(build_id=build.id, version=version, options=body.model_dump(exclude={"build_id", "version"}))
    db.add(ds)
    db.flush()
    db.commit()
    enqueue("dataset", ds.id, background)
    return ds


@router.get("", response_model=list[DatasetOut])
def list_datasets(db: Session = Depends(get_db)) -> list[Dataset]:
    return list(db.execute(select(Dataset).order_by(Dataset.id.desc())).scalars())


@router.get("/{dataset_id}", response_model=DatasetOut)
def get_dataset(dataset_id: int, db: Session = Depends(get_db)) -> Dataset:
    ds = db.get(Dataset, dataset_id)
    if ds is None:
        raise HTTPException(404, "dataset not found")
    return ds


@router.get("/{dataset_id}/questions", response_model=list[QuestionOut])
def list_questions(dataset_id: int, family: str | None = None, db: Session = Depends(get_db)) -> list[Question]:
    query = select(Question).where(Question.dataset_id == dataset_id).order_by(Question.id)
    if family:
        query = query.where(Question.family == family)
    return list(db.execute(query).scalars())


@router.get("/{dataset_id}/export", response_class=PlainTextResponse)
def export_dataset(dataset_id: int, db: Session = Depends(get_db)) -> str:
    """The dataset as JSONL, the format of the frozen sets in evals/datasets/."""
    questions = db.execute(select(Question).where(Question.dataset_id == dataset_id).order_by(Question.id)).scalars()
    return "\n".join(json.dumps(question_record(q)) for q in questions) + "\n"
