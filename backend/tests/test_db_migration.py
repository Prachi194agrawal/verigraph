"""Databases created by an older version gain new columns at startup; legacy builds keep their Neo4j key."""

from __future__ import annotations

import sqlite3

from app import db as db_module
from app.config import get_settings


def test_init_db_adds_graph_id_and_keeps_legacy_keys(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE repos (id INTEGER PRIMARY KEY, url VARCHAR(500) NOT NULL, name VARCHAR(200) NOT NULL,
                            commit_sha VARCHAR(64), created_at DATETIME NOT NULL);
        CREATE TABLE graph_builds (id INTEGER PRIMARY KEY, repo_id INTEGER NOT NULL, repo VARCHAR(500) NOT NULL,
                                   commit_sha VARCHAR(64), parser_version VARCHAR(50) NOT NULL,
                                   status VARCHAR(20) NOT NULL);
        INSERT INTO repos VALUES (1, 'https://example.com/r', 'r', NULL, '2026-01-01');
        INSERT INTO graph_builds VALUES (7, 1, 'r', NULL, 'v0', 'done');
    """)
    con.commit()
    con.close()
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{path}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    db_module._engine = None
    try:
        db_module.init_db()
        db_module.init_db()  # idempotent
        from app.models import GraphBuild

        with db_module.session_scope() as db:
            legacy = db.get(GraphBuild, 7)
            assert legacy.graph_id == 7  # its graph was written under its row id
            assert legacy.progress is None and legacy.stats is None  # other added columns are nullable
            new = GraphBuild(repo_id=1, repo="r", parser_version="v1")
            db.add(new)
            db.flush()
            assert new.graph_id >= 1_000_000_000
    finally:
        get_settings.cache_clear()
        db_module._engine = None
