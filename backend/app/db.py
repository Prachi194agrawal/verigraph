"""SQLAlchemy engine and sessions for the results database (PostgreSQL, or SQLite locally)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Connection, create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    global _engine, _session_factory
    if _engine is None:
        url = get_settings().db_url
        kwargs: dict = {"pool_pre_ping": True}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
        _engine = create_engine(url, **kwargs)
        if url.startswith("sqlite"):

            @event.listens_for(_engine, "connect")
            def _sqlite_pragmas(dbapi_conn, _record):  # WAL lets the API read while a job writes
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA foreign_keys=ON")
                cur.close()

        _session_factory = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def _add_missing_columns(conn: Connection) -> None:
    """Additive schema upgrades for databases created by an older version: add any column the models have
    and the table lacks (as nullable). Anything beyond additive changes would need a real migration tool."""
    from app.models import Base

    inspector = inspect(conn)
    existing_tables = set(inspector.get_table_names())
    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue
        present = {c["name"] for c in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in present:
                continue
            conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN {column.name} '
                              f'{column.type.compile(dialect=conn.dialect)}'))
            if table.name == "graph_builds" and column.name == "graph_id":
                # Builds written before graph ids existed used their row id as the Neo4j key.
                conn.execute(text("UPDATE graph_builds SET graph_id = id"))
                conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_graph_builds_graph_id "
                                  "ON graph_builds (graph_id)"))


def init_db() -> None:
    """Create missing tables and columns. The API and the worker both call this at startup; on PostgreSQL an
    advisory lock serializes them, because two concurrent CREATE TABLE statements for the same table fail."""
    from app.models import Base

    engine = get_engine()
    with engine.begin() as conn:
        if engine.dialect.name == "postgresql":
            conn.execute(text("SELECT pg_advisory_xact_lock(724411)"))
        _add_missing_columns(conn)
        Base.metadata.create_all(conn)


@contextmanager
def session_scope() -> Iterator[Session]:
    get_engine()
    assert _session_factory is not None
    session = _session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency."""
    with session_scope() as session:
        yield session
