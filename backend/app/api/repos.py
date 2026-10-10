"""POST /repos (register + pin a commit) and POST /repos/{id}/builds (build the code graph)."""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import PARSER_VERSION
from app.config import get_settings
from app.db import get_db
from app.ingest.clone import local_source_allowed, repo_name
from app.models import GraphBuild, Repo
from app.schemas import BuildCreate, BuildOut, RepoCreate, RepoOut
from app.workers.dispatch import enqueue

router = APIRouter(prefix="/repos", tags=["repos"])


def _check_source(url: str) -> None:
    if not local_source_allowed(url, get_settings().local_roots):
        raise HTTPException(403, "local repository paths must be inside LOCAL_REPO_ROOTS; use a Git URL instead")


@router.post("", response_model=RepoOut, status_code=201)
def create_repo(body: RepoCreate, db: Session = Depends(get_db)) -> Repo:
    _check_source(body.url)
    repo = Repo(url=body.url.strip(), name=body.name or repo_name(body.url), commit_sha=body.commit_sha)
    db.add(repo)
    db.flush()
    return repo


@router.get("", response_model=list[RepoOut])
def list_repos(db: Session = Depends(get_db)) -> list[Repo]:
    return list(db.execute(select(Repo).order_by(Repo.id.desc())).scalars())


@router.get("/{repo_id}", response_model=RepoOut)
def get_repo(repo_id: int, db: Session = Depends(get_db)) -> Repo:
    repo = db.get(Repo, repo_id)
    if repo is None:
        raise HTTPException(404, "repo not found")
    return repo


@router.post("/{repo_id}/builds", response_model=BuildOut, status_code=202)
def create_build(repo_id: int, body: BuildCreate, background: BackgroundTasks,
                 db: Session = Depends(get_db)) -> GraphBuild:
    repo = db.get(Repo, repo_id)
    if repo is None:
        raise HTTPException(404, "repo not found")
    _check_source(repo.url)  # the allowlist may have been tightened after the repo was registered
    build = GraphBuild(repo_id=repo.id, repo=repo.name, commit_sha=None, parser_version=PARSER_VERSION,
                       options=body.model_dump(exclude_none=True))
    db.add(build)
    db.flush()
    db.commit()
    enqueue("build", build.id, background)
    return build


@router.get("/{repo_id}/builds", response_model=list[BuildOut])
def list_repo_builds(repo_id: int, db: Session = Depends(get_db)) -> list[GraphBuild]:
    return list(db.execute(select(GraphBuild).where(GraphBuild.repo_id == repo_id)
                           .order_by(GraphBuild.id.desc())).scalars())
