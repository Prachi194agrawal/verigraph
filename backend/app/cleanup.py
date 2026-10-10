"""Delete a build everywhere: its graph in Neo4j, its checkout on disk, and its rows in the results database
(datasets, questions, experiments, answers and every check, score and label that hangs off them)."""

from __future__ import annotations

import shutil
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.eval.index import evict_build_index
from app.graph.store import GraphStore
from app.models import (
    Answer,
    ClaimVerdict,
    Dataset,
    EdgeAudit,
    Experiment,
    GraphBuild,
    HumanLabel,
    Question,
    Score,
    SemanticAudit,
    SymbolCheck,
    TestRun,
)

ACTIVE = ("queued", "running")


def active_jobs(db: Session, build_id: int) -> list[str]:
    """Jobs of this build that may still be running in a worker."""
    jobs = []
    build = db.get(GraphBuild, build_id)
    if build is not None and build.status in ACTIVE:
        jobs.append(f"build {build_id}")
    datasets = select(Dataset.id).where(Dataset.build_id == build_id)
    jobs += [f"dataset {i}" for i in db.execute(select(Dataset.id).where(
        Dataset.build_id == build_id, Dataset.status.in_(ACTIVE))).scalars()]
    jobs += [f"experiment {i}" for i in db.execute(select(Experiment.id).where(
        Experiment.dataset_id.in_(datasets), Experiment.status.in_(ACTIVE))).scalars()]
    return jobs


def delete_build(db: Session, store: GraphStore, build_id: int, data_dir: Path) -> dict:
    """Neo4j first: if the graph cannot be removed, the results stay and the call can be retried."""
    build = db.get(GraphBuild, build_id)
    graph_id = build.graph_id if build is not None else build_id
    store.delete_build(graph_id)
    evict_build_index(graph_id)
    datasets = select(Dataset.id).where(Dataset.build_id == build_id)
    experiments = select(Experiment.id).where(Experiment.dataset_id.in_(datasets))
    answers = select(Answer.id).where(Answer.experiment_id.in_(experiments))
    counts = {"answers": len(db.execute(answers).scalars().all())}
    for table in (HumanLabel, ClaimVerdict, SymbolCheck, TestRun, Score):
        db.execute(delete(table).where(table.answer_id.in_(answers)))
    db.execute(delete(Answer).where(Answer.experiment_id.in_(experiments)))
    counts["experiments"] = db.execute(delete(Experiment).where(Experiment.dataset_id.in_(datasets))).rowcount
    db.execute(delete(Question).where(Question.dataset_id.in_(datasets)))
    counts["datasets"] = db.execute(delete(Dataset).where(Dataset.build_id == build_id)).rowcount
    for table in (EdgeAudit, SemanticAudit):
        db.execute(delete(table).where(table.build_id == build_id))
    db.execute(delete(GraphBuild).where(GraphBuild.id == build_id))
    shutil.rmtree(data_dir / "builds" / str(build_id), ignore_errors=True)
    return {"deleted_build": build_id, **counts}
