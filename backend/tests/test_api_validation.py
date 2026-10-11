"""Reject invalid work before it reaches a worker or contaminates human agreement scores."""

from __future__ import annotations

import pytest
from fastapi import BackgroundTasks, HTTPException
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.api.experiments import create_experiments, resume_experiment
from app.api.results import add_label
from app.models import Answer, Base, ClaimVerdict, Dataset, Experiment, GraphBuild, HumanLabel
from app.schemas import BuildCreate, DatasetCreate, ExperimentCreate, LabelCreate, RepoCreate


def test_registered_repo_visible_before_request_cleanup(tmp_path):
    from app.api.repos import create_repo, get_repo

    engine = create_engine(f"sqlite:///{tmp_path / 'registration.db'}")
    Base.metadata.create_all(engine)
    try:
        with Session(engine, expire_on_commit=False) as registration:
            repo = create_repo(RepoCreate(url="https://github.com/psf/requests"), registration)
            # A following request has its own connection while the first session
            # is still open and its dependency cleanup has not yet run.
            with Session(engine) as next_request:
                assert get_repo(repo.id, next_request).url == repo.url
    finally:
        engine.dispose()


@pytest.mark.parametrize("schema,payload", [
    (RepoCreate, {"url": "  "}),
    (BuildCreate, {"resolvers": ["unknown"]}),
    (BuildCreate, {"resolvers": []}),
    (BuildCreate, {"max_commits": 0}),
    (DatasetCreate, {"build_id": 1, "per_family": 0}),
    (ExperimentCreate, {"dataset_id": 1, "models": []}),
    (ExperimentCreate, {"dataset_id": 1, "models": [" "]}),
    (ExperimentCreate, {"dataset_id": 1, "models": ["mock/empty"], "pipelines": []}),
    (ExperimentCreate, {"dataset_id": 1, "models": ["mock/empty"], "concurrency": 0}),
    (ExperimentCreate, {"dataset_id": 1, "models": ["mock/empty"], "concurrency": 33}),
    (ExperimentCreate, {"dataset_id": 1, "models": ["mock/empty"], "limit": -1}),
])
def test_invalid_job_input(schema, payload):
    with pytest.raises(ValidationError):
        schema(**payload)


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def test_vector_requires_embeddings(db):
    db.add(GraphBuild(id=1, repo_id=1, repo="mini", parser_version="test", stats={}))
    db.add(Dataset(id=1, build_id=1, version="test", status="done"))
    db.flush()
    with pytest.raises(HTTPException) as exc:
        create_experiments(ExperimentCreate(dataset_id=1, models=["mock/empty"]), BackgroundTasks(), db)
    assert exc.value.status_code == 409
    assert not db.execute(select(Experiment)).first()


@pytest.mark.parametrize("status", ["queued", "running", "done"])
def test_resume_does_not_duplicate_active_or_completed_jobs(db, status):
    db.add(Experiment(id=1, dataset_id=1, model="mock/empty", pipeline="none", prompt_version="v1", status=status))
    db.flush()
    with pytest.raises(HTTPException) as exc:
        resume_experiment(1, BackgroundTasks(), db)
    assert exc.value.status_code == 409


def test_label_cannot_reference_another_answers_claim(db):
    db.add(Answer(id=1, experiment_id=1, question_id=1))
    db.add(ClaimVerdict(id=1, answer_id=2, subject="a", relation="calls", object="b", verdict="supported"))
    db.flush()
    with pytest.raises(HTTPException) as exc:
        add_label(1, LabelCreate(label="supported", claim_verdict_id=1), db)
    assert exc.value.status_code == 422
    assert not db.execute(select(HumanLabel)).first()


def test_label_accepts_its_own_claim(db):
    db.add(Answer(id=1, experiment_id=1, question_id=1))
    db.add(ClaimVerdict(id=1, answer_id=1, subject="a", relation="calls", object="b", verdict="supported"))
    db.flush()
    result = add_label(1, LabelCreate(label="supported", claim_verdict_id=1), db)
    assert result["id"] is not None


def test_local_repository_paths_are_confined_to_allowed_roots(tmp_path):
    from app.ingest.clone import local_source, local_source_allowed

    allowed = tmp_path / "repos"
    (allowed / "proj").mkdir(parents=True)
    roots = [allowed.resolve()]
    assert local_source_allowed(str(allowed / "proj"), roots)
    assert local_source_allowed(f"file://{allowed / 'proj'}", roots)
    assert not local_source_allowed("/etc", roots)
    assert not local_source_allowed(str(allowed / "proj" / ".." / ".." / "secrets"), roots)  # no ../ escape
    assert not local_source_allowed(f"file://{tmp_path}", roots)
    for remote in ("https://github.com/psf/requests", "git@github.com:psf/requests.git", "ssh://host/r.git"):
        assert local_source(remote) is None and local_source_allowed(remote, roots)
    assert local_source_allowed("/anything", [])  # no roots configured: local development


def test_api_rejects_local_paths_outside_the_roots(db, monkeypatch, tmp_path):
    from app.api.repos import create_repo
    from app.config import get_settings

    monkeypatch.setenv("LOCAL_REPO_ROOTS", str(tmp_path))
    get_settings.cache_clear()
    try:
        with pytest.raises(HTTPException) as exc:
            create_repo(RepoCreate(url="/etc"), db)
        assert exc.value.status_code == 403
        assert create_repo(RepoCreate(url=str(tmp_path)), db).url == str(tmp_path)
    finally:
        get_settings.cache_clear()
