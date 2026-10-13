"""Authentication and durable budgets must reject work before dispatch."""

from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, select

from app.access import AccessMiddleware
from app.config import Settings
from app.models import Base, UsageCounter
from app.usage import consume


@pytest.fixture
def access_client(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'usage.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    settings = Settings(_env_file=None, data_dir=tmp_path, api_users={"alice": SecretStr("alice-secret"),
                        "bob": SecretStr("bob-secret")}, api_daily_write_limit=2)
    monkeypatch.setattr("app.access.get_settings", lambda: settings)
    monkeypatch.setattr("app.usage.get_engine", lambda: engine)
    app = FastAPI()
    app.add_middleware(AccessMiddleware)

    @app.api_route("/repos", methods=["GET", "POST", "DELETE"])
    def repos(request: Request):
        return {"username": request.state.username}

    @app.get("/health")
    def health():
        return {"database": "ok"}

    with TestClient(app) as client:
        yield client, settings, engine
    engine.dispose()


def test_private_reads_and_writes_require_valid_credentials(access_client):
    client, _, _ = access_client
    assert client.get("/health").status_code == 200
    for method in (client.get, client.post, client.delete):
        assert method("/repos").status_code == 401
        assert method("/repos", auth=("alice", "wrong")).status_code == 401
    assert client.get("/repos", auth=("alice", "alice-secret")).json()["username"] == "alice"
    assert client.get("/repos", headers={"Authorization": "Basic invalid!"}).status_code == 401


def test_demo_allows_anonymous_reads_but_no_writes(access_client):
    client, settings, _ = access_client
    settings.demo_read_only = True
    assert client.get("/repos").status_code == 200
    assert client.post("/repos").status_code == 401
    assert client.delete("/repos").status_code == 401
    assert client.post("/repos", auth=("alice", "alice-secret")).status_code == 200


def test_write_quota_is_per_user_and_durable(access_client):
    client, _, engine = access_client
    for _ in range(2):
        assert client.post("/repos", auth=("alice", "alice-secret")).status_code == 200
    limited = client.post("/repos", auth=("alice", "alice-secret"))
    assert limited.status_code == 429 and int(limited.headers["Retry-After"]) > 0
    assert client.get("/repos", auth=("alice", "alice-secret")).status_code == 200
    assert client.post("/repos", auth=("bob", "bob-secret")).status_code == 200
    with engine.connect() as conn:
        assert conn.scalar(select(UsageCounter.count).where(UsageCounter.principal == "api:alice")) == 2


def test_quota_is_atomic_across_concurrent_callers(access_client):
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: consume("concurrent", 5), range(20)))
    assert sum(results) == 5


def test_cross_origin_writes_are_rejected_before_consuming_quota(access_client):
    client, _, _ = access_client
    assert client.post("/repos", auth=("alice", "alice-secret"),
                       headers={"Origin": "https://untrusted.example"}).status_code == 403
    assert client.post("/repos", auth=("alice", "alice-secret"),
                       headers={"Origin": "http://localhost:3000"}).status_code == 200


def test_required_auth_fails_closed_without_users(access_client):
    client, settings, _ = access_client
    settings.api_users = {}
    settings.require_auth = True
    settings.demo_read_only = True
    assert client.get("/repos").status_code == 503
