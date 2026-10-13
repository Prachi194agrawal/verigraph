"""Durable, concurrency-safe quotas for SQLite and PostgreSQL."""

from datetime import UTC, datetime, timedelta

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.db import get_engine
from app.models import UsageCounter


def retry_after() -> int:
    now = datetime.now(UTC)
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(1, int((midnight - now).total_seconds()))


def consume(principal: str, limit: int) -> bool:
    """Reserve one attempt, even if it later fails. A restart cannot reset the quota."""
    engine = get_engine()
    insert = sqlite_insert if engine.dialect.name == "sqlite" else pg_insert
    stmt = insert(UsageCounter).values(principal=principal, day=datetime.now(UTC).date().isoformat(), count=1)
    stmt = stmt.on_conflict_do_update(
        index_elements=[UsageCounter.principal, UsageCounter.day],
        set_={"count": UsageCounter.count + 1}, where=UsageCounter.count < limit,
    ).returning(UsageCounter.count)
    with engine.begin() as conn:
        return conn.execute(stmt).scalar_one_or_none() is not None
