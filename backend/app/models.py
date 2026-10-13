"""Results database tables: runs, questions, answers, verdicts and labels.

The code graph itself lives in Neo4j; this database holds everything about *evaluating* models on it.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_graph_id() -> int:
    """A random key for a build's nodes in Neo4j (their `build_id` property). It is independent of the row id,
    so several results databases (say, the Docker stack's PostgreSQL and a local SQLite file) can share one
    Neo4j without overwriting each other's graphs. Kept below 2**53 so browsers can hold it exactly."""
    return 1_000_000_000 + secrets.randbelow(999_000_000_000)


class Base(DeclarativeBase):
    pass


class UsageCounter(Base):
    """Atomic daily counters shared by API processes and workers; days use UTC."""
    __tablename__ = "usage_counters"

    principal: Mapped[str] = mapped_column(String(250), primary_key=True)
    day: Mapped[str] = mapped_column(String(10), primary_key=True)
    count: Mapped[int] = mapped_column(Integer, default=0)


class Repo(Base):
    __tablename__ = "repos"

    id: Mapped[int] = mapped_column(primary_key=True)
    url: Mapped[str] = mapped_column(String(500))
    name: Mapped[str] = mapped_column(String(200))
    commit_sha: Mapped[str | None] = mapped_column(String(64))  # requested pin; None means HEAD at build time
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    builds: Mapped[list[GraphBuild]] = relationship(back_populates="repository")


class GraphBuild(Base):
    __tablename__ = "graph_builds"

    id: Mapped[int] = mapped_column(primary_key=True)
    graph_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True, default=new_graph_id)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repos.id"), index=True)
    repo: Mapped[str] = mapped_column(String(500))  # repo name, kept on the row for easy reporting
    commit_sha: Mapped[str | None] = mapped_column(String(64))  # resolved commit the graph is pinned to
    parser_version: Mapped[str] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(20), default="queued")  # queued | running | done | failed
    progress: Mapped[str | None] = mapped_column(Text)
    options: Mapped[dict] = mapped_column(JSON, default=dict)
    stats: Mapped[dict | None] = mapped_column(JSON)
    unresolved_call_rate: Mapped[float | None] = mapped_column(Float)
    checkout_path: Mapped[str | None] = mapped_column(String(1000))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    repository: Mapped[Repo] = relationship(back_populates="builds")


class Dataset(Base):
    __tablename__ = "datasets"

    id: Mapped[int] = mapped_column(primary_key=True)
    build_id: Mapped[int] = mapped_column(ForeignKey("graph_builds.id"), index=True)
    version: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(20), default="queued")
    progress: Mapped[str | None] = mapped_column(Text)
    options: Mapped[dict] = mapped_column(JSON, default=dict)
    n_questions: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Question(Base):
    __tablename__ = "questions"

    id: Mapped[int] = mapped_column(primary_key=True)
    dataset_id: Mapped[int] = mapped_column(ForeignKey("datasets.id"), index=True)
    dataset_version: Mapped[str] = mapped_column(String(100))
    family: Mapped[str] = mapped_column(String(40), index=True)
    text: Mapped[str] = mapped_column(Text)
    template_text: Mapped[str] = mapped_column(Text)  # the text before LLM rephrasing
    gold_cypher: Mapped[str] = mapped_column(Text)
    gold_params: Mapped[dict] = mapped_column(JSON, default=dict)
    gold_answer: Mapped[list] = mapped_column(JSON, default=list)
    hop_count: Mapped[int | None] = mapped_column(Integer)
    target: Mapped[str | None] = mapped_column(String(500))
    meta: Mapped[dict] = mapped_column(JSON, default=dict)


class Experiment(Base):
    __tablename__ = "experiments"

    id: Mapped[int] = mapped_column(primary_key=True)
    dataset_id: Mapped[int] = mapped_column(ForeignKey("datasets.id"), index=True)
    model: Mapped[str] = mapped_column(String(200))
    pipeline: Mapped[str] = mapped_column(String(40))
    prompt_version: Mapped[str] = mapped_column(String(40))
    judge_model: Mapped[str | None] = mapped_column(String(200))
    run_group: Mapped[str | None] = mapped_column(String(64), index=True)
    options: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="queued")
    progress: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[dict | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Answer(Base):
    __tablename__ = "answers"

    id: Mapped[int] = mapped_column(primary_key=True)
    experiment_id: Mapped[int] = mapped_column(ForeignKey("experiments.id"), index=True)
    question_id: Mapped[int] = mapped_column(ForeignKey("questions.id"), index=True)
    text: Mapped[str] = mapped_column(Text, default="")
    parsed_items: Mapped[list] = mapped_column(JSON, default=list)  # normalized to canonical graph names
    match: Mapped[dict] = mapped_column(JSON, default=dict)  # raw items, true/false positives, false negatives
    explanation: Mapped[str | None] = mapped_column(Text)
    code: Mapped[str | None] = mapped_column(Text)
    retrieved_nodes: Mapped[list] = mapped_column(JSON, default=list)
    steps: Mapped[list] = mapped_column(JSON, default=list)  # Cypher-agent queries and row counts
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    error: Mapped[str | None] = mapped_column(Text)
    trace_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ClaimVerdict(Base):
    __tablename__ = "claim_verdicts"

    id: Mapped[int] = mapped_column(primary_key=True)
    answer_id: Mapped[int] = mapped_column(ForeignKey("answers.id"), index=True)
    subject: Mapped[str] = mapped_column(String(500))
    relation: Mapped[str] = mapped_column(String(40))
    object: Mapped[str] = mapped_column(String(500))
    verdict: Mapped[str] = mapped_column(String(20))  # supported | contradicted | unverifiable
    evidence_edge: Mapped[str | None] = mapped_column(Text)
    extractor: Mapped[str] = mapped_column(String(20), default="rule")  # rule | llm
    claim_text: Mapped[str | None] = mapped_column(Text)


class SymbolCheck(Base):
    __tablename__ = "symbol_checks"

    id: Mapped[int] = mapped_column(primary_key=True)
    answer_id: Mapped[int] = mapped_column(ForeignKey("answers.id"), index=True)
    symbol: Mapped[str] = mapped_column(String(500))
    kind: Mapped[str] = mapped_column(String(20))  # import | attribute | call | name
    exists: Mapped[bool] = mapped_column(Boolean)
    arity_ok: Mapped[bool | None] = mapped_column(Boolean)
    category: Mapped[str | None] = mapped_column(String(40))  # missing_symbol | wrong_arity | ...
    detail: Mapped[str | None] = mapped_column(Text)
    line: Mapped[int | None] = mapped_column(Integer)


class TestRun(Base):
    __tablename__ = "test_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    answer_id: Mapped[int] = mapped_column(ForeignKey("answers.id"), index=True)
    tests_run: Mapped[int] = mapped_column(Integer, default=0)
    passed: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20))  # passed | failed | error | skipped
    log_path: Mapped[str | None] = mapped_column(String(1000))


class Score(Base):
    __tablename__ = "scores"

    id: Mapped[int] = mapped_column(primary_key=True)
    answer_id: Mapped[int] = mapped_column(ForeignKey("answers.id"), index=True)
    metric: Mapped[str] = mapped_column(String(60), index=True)
    value: Mapped[float] = mapped_column(Float)


class HumanLabel(Base):
    """A human judgement. With claim_verdict_id it labels one extracted claim
    (supported/contradicted/unverifiable); without it, it labels the whole answer (correct/incorrect)."""

    __tablename__ = "human_labels"

    id: Mapped[int] = mapped_column(primary_key=True)
    answer_id: Mapped[int] = mapped_column(ForeignKey("answers.id"), index=True)
    claim_verdict_id: Mapped[int | None] = mapped_column(ForeignKey("claim_verdicts.id"))
    label: Mapped[str] = mapped_column(String(40))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EdgeAudit(Base):
    """A sampled CALLS edge waiting for (or carrying) a hand check, used to measure resolver precision."""

    __tablename__ = "edge_audits"

    id: Mapped[int] = mapped_column(primary_key=True)
    build_id: Mapped[int] = mapped_column(ForeignKey("graph_builds.id"), index=True)
    caller: Mapped[str] = mapped_column(String(500))
    callee: Mapped[str] = mapped_column(String(500))
    resolved_by: Mapped[str] = mapped_column(String(20))
    file_path: Mapped[str | None] = mapped_column(String(1000))
    line: Mapped[int | None] = mapped_column(Integer)
    label: Mapped[str | None] = mapped_column(String(20))  # correct | incorrect | None (not checked yet)
    notes: Mapped[str | None] = mapped_column(Text)


class SemanticAudit(Base):
    """An LLM-layer item (summary or feature tag) graded by a judge model and optionally by a human."""

    __tablename__ = "semantic_audits"

    id: Mapped[int] = mapped_column(primary_key=True)
    build_id: Mapped[int] = mapped_column(ForeignKey("graph_builds.id"), index=True)
    function_qn: Mapped[str] = mapped_column(String(500))
    kind: Mapped[str] = mapped_column(String(20))  # summary | feature
    content: Mapped[str] = mapped_column(Text)
    judge_model: Mapped[str | None] = mapped_column(String(200))
    judge_label: Mapped[str | None] = mapped_column(String(20))  # faithful | unfaithful
    human_label: Mapped[str | None] = mapped_column(String(20))
