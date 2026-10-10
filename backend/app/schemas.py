"""Request and response bodies of the HTTP API."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class RepoCreate(BaseModel):
    url: str = Field(description="Git URL, GitHub URL, or a local path (git repo or plain folder)")
    commit_sha: str | None = Field(None, description="Commit, tag or branch to pin; default: HEAD at build time")
    name: str | None = None

    @field_validator("url")
    @classmethod
    def nonempty_url(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("repository URL or path must not be empty")
        return value


class RepoOut(_Out):
    id: int
    url: str
    name: str
    commit_sha: str | None
    created_at: datetime


class BuildCreate(BaseModel):
    commit_sha: str | None = None
    resolvers: list[str] | None = Field(None, description="Order of call resolvers, e.g. [jedi, import, name]")
    history: bool = True
    github: bool = True
    max_commits: int | None = Field(None, ge=1)
    embeddings: bool = True
    embedding_model: str | None = None
    semantic_model: str | None = Field(None, description="LLM for summaries + feature tags; omit to skip")
    semantic_limit: int | None = Field(None, ge=1)

    @field_validator("resolvers")
    @classmethod
    def known_resolvers(cls, value: list[str] | None) -> list[str] | None:
        from app.config import ALL_RESOLVERS

        if value is not None and (not value or set(value) - set(ALL_RESOLVERS)):
            raise ValueError(f"choose one or more resolvers from {ALL_RESOLVERS}")
        return value


class BuildOut(_Out):
    id: int
    graph_id: int  # the `build_id` property of this build's nodes in Neo4j
    repo_id: int
    repo: str
    commit_sha: str | None
    parser_version: str
    status: str
    progress: str | None
    options: dict
    stats: dict | None
    unresolved_call_rate: float | None
    error: str | None
    created_at: datetime
    finished_at: datetime | None


class DatasetCreate(BaseModel):
    build_id: int
    families: list[str] | None = None
    per_family: int = Field(20, ge=1, le=1000)
    seed: int = 0
    rephrase_model: str | None = None
    version: str | None = None


class DatasetOut(_Out):
    id: int
    build_id: int
    version: str
    status: str
    progress: str | None
    options: dict
    n_questions: int
    error: str | None
    created_at: datetime
    finished_at: datetime | None


class QuestionOut(_Out):
    id: int
    dataset_id: int
    dataset_version: str
    family: str
    text: str
    template_text: str
    gold_cypher: str
    gold_params: dict
    gold_answer: list
    hop_count: int | None
    target: str | None
    meta: dict


class ExperimentCreate(BaseModel):
    dataset_id: int
    models: list[str] = Field(min_length=1, description='LiteLLM model names, e.g. "groq/llama-3.1-8b-instant"')
    pipelines: list[str] = Field(default=["none", "vector", "graphrag", "cypher_agent"], min_length=1)
    prompt_version: str = "v1"
    judge_model: str | None = None
    limit: int | None = Field(None, ge=1)
    families: list[str] | None = None
    concurrency: int = Field(4, ge=1, le=32)
    run_tests: bool = False
    options: dict = Field(default_factory=dict, description="top_k, graphrag_hops, agent_max_queries, ...")

    @field_validator("models", "pipelines")
    @classmethod
    def nonempty_names(cls, value: list[str]) -> list[str]:
        names = [name.strip() for name in value]
        if any(not name for name in names):
            raise ValueError("model and pipeline names must not be empty")
        return list(dict.fromkeys(names))


class ExperimentOut(_Out):
    id: int
    dataset_id: int
    model: str
    pipeline: str
    prompt_version: str
    judge_model: str | None
    run_group: str | None
    options: dict
    status: str
    progress: str | None
    summary: dict | None
    error: str | None
    created_at: datetime
    finished_at: datetime | None


class LabelCreate(BaseModel):
    label: str = Field(description="correct/incorrect for an answer; supported/contradicted/unverifiable for a claim")
    claim_verdict_id: int | None = None
    notes: str | None = None


class AuditLabel(BaseModel):
    label: str = Field(pattern="^(correct|incorrect)$")
    notes: str | None = None


class AskRequest(BaseModel):
    question: str = Field(min_length=3, max_length=8000)
    model: str = Field(min_length=1, description='LiteLLM model name, e.g. "groq/llama-3.1-8b-instant"')
    pipeline: str = "graphrag"
    prompt_version: str = "v1"
    judge_model: str | None = Field(None, description="optional LLM claim extractor, in addition to the rules")
    options: dict = Field(default_factory=dict, description="top_k, graphrag_hops, agent_max_queries, ...")


class ImpactRequest(BaseModel):
    diff: str | None = Field(None, description="unified diff against the build's commit (e.g. a PR's base)")
    functions: list[str] = Field(default_factory=list, description="changed functions, e.g. Session.send")
    max_hops: int = Field(3, ge=1, le=6)
