import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from app.graph.store import is_read_only
from app.llm import client as client_module
from app.llm.cache import ResponseCache, cache_key
from app.llm.client import LLMClient, extract_json
from app.llm.mock import mock_complete
from app.llm.prompts import render
from app.llm.tracing import Tracer
from app.pipelines.parsing import extract_code, parse_output


def test_extract_json_variants():
    assert extract_json('{"answer": ["a"]}') == {"answer": ["a"]}
    assert extract_json('Sure!\n```json\n{"answer": ["a", "b"]}\n```') == {"answer": ["a", "b"]}
    assert extract_json('Here: {"answer": ["x\\"y"], "n": {"k": 1}} trailing') == {"answer": ['x"y'], "n": {"k": 1}}
    assert extract_json("no json here") is None


def test_parse_output_shapes():
    assert parse_output('{"answer": ["a.b", {"qualified_name": "c.d"}], "explanation": "e"}', "callers") == (
        ["a.b", "c.d"], "e", None)
    assert parse_output('{"answer": "a.b, c.d"}', "callers")[0] == ["a.b", "c.d"]
    assert parse_output('["x"]', "lookup")[0] == ["x"]
    assert parse_output("I cannot answer", "callers") == ([], "I cannot answer", None)
    assert extract_code("text\n```python\ndef f():\n    pass\n```\nmore") == "def f():\n    pass"
    assert parse_output("```python\ndef g(): return 1\n```", "codegen")[2] == "def g(): return 1"


def test_read_only_guard():
    assert is_read_only("MATCH (n) RETURN n.name AS created_at")
    assert is_read_only("MATCH (n) WHERE n.title CONTAINS 'CREATE' RETURN n")  # inside a string literal
    assert not is_read_only("MATCH (n) DETACH DELETE n")
    assert not is_read_only("CREATE (n:X)")
    assert not is_read_only("MATCH (n) SET n.x = 1")
    assert not is_read_only("CALL apoc.periodic.iterate('a', 'b', {})")


def test_prompts_render():
    text = render("cypher_agent", repo="r", commit="c", schema="S", max_rows=5, max_queries=2)
    assert "{build_id: $build_id}" in text and "at most 2 queries" in text


def test_mock_models():
    msgs = [{"role": "system", "content": "answer"}, {"role": "user", "content":
            "Context from the repository:\n\npkg.a -CALLS-> pkg.target [jedi]\npkg.b -CALLS-> pkg.other [jedi]\n\n"
            "Question: Which functions call `target`?"}]
    assert json.loads(mock_complete("mock/extractive", msgs))["answer"] == ["pkg.a"]
    assert json.loads(mock_complete("mock/empty", msgs))["answer"] == []


def test_client_caches_and_traces(tmp_path):
    from app.config import Settings

    settings = Settings(data_dir=tmp_path, llm_cache=True)
    client = LLMClient(settings, Tracer(tmp_path / "traces"))
    msgs = [{"role": "user", "content": "Question: Which functions call `x`?"}]
    first = client.complete("mock/empty", msgs, trace_id="t1")
    second = client.complete("mock/empty", msgs, trace_id="t1")
    assert not first.cached and second.cached and first.text == second.text
    lines = (tmp_path / "traces").glob("*.jsonl")
    assert sum(len(p.read_text().splitlines()) for p in lines) == 2


class _RateLimited(Exception):
    """What LiteLLM raises on an HTTP 429."""

    status_code = 429

    def __init__(self, retry_after: str | None = None, wait: str = "2.5s") -> None:
        super().__init__(f"Rate limit reached for model. Please try again in {wait}.")
        self.response = SimpleNamespace(headers={"retry-after": retry_after} if retry_after else {})


def _reply(text: str) -> SimpleNamespace:
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
                           usage=SimpleNamespace(prompt_tokens=3, completion_tokens=1))


def _fake_litellm(outcomes: list) -> SimpleNamespace:
    """Stands in for the litellm module: each completion() raises or returns the next outcome."""
    calls: list[dict] = []

    def completion(**kwargs):
        calls.append(kwargs)
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return _reply(outcome)
    return SimpleNamespace(completion=completion, completion_cost=lambda **_: 0.0, calls=calls)


def test_rate_limited_calls_wait_and_retry(tmp_path):
    from app.config import Settings

    client = LLMClient(Settings(data_dir=tmp_path, llm_cache=False, llm_rate_limit_retries=2),
                       Tracer(tmp_path / "traces"))
    waits: list[float] = []
    client._sleep = waits.append
    client._litellm = _fake_litellm([_RateLimited("3"), _RateLimited(), "ok"])
    assert client.complete("groq/m", [{"role": "user", "content": "a"}]).text == "ok"
    assert len(client._litellm.calls) == 3
    assert 3 <= waits[0] <= 4  # the provider's Retry-After header
    assert 2.5 <= waits[1] <= 3.5  # "try again in 2.5s" in the error message

    client._litellm = _fake_litellm([_RateLimited(), _RateLimited(), _RateLimited(), "never"])
    with pytest.raises(_RateLimited):  # retries are bounded
        client.complete("groq/m", [{"role": "user", "content": "b"}])
    assert len(client._litellm.calls) == 3

    client._litellm = _fake_litellm([ValueError("bad request"), "never"])
    with pytest.raises(ValueError):  # only rate limits are waited out
        client.complete("groq/m", [{"role": "user", "content": "c"}])
    assert len(client._litellm.calls) == 1

    waits.clear()
    client._litellm = _fake_litellm([_RateLimited(wait="7m30.5s"), "never"])
    with pytest.raises(_RateLimited):  # a used-up daily quota: waiting a minute would not help
        client.complete("groq/m", [{"role": "user", "content": "d"}])
    assert len(client._litellm.calls) == 1 and waits == []
    assert client_module._retry_after_s(_RateLimited(wait="1h2m3s")) == 3723
    assert client_module._retry_after_s(_RateLimited(wait="450ms")) == 0.45


def test_daily_budget_counts_retries_but_excludes_cached_and_mock_calls(tmp_path, monkeypatch):
    from sqlalchemy import create_engine

    from app.config import Settings
    from app.models import Base

    engine = create_engine(f"sqlite:///{tmp_path / 'budget.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr("app.usage.get_engine", lambda: engine)
    settings = Settings(_env_file=None, data_dir=tmp_path, llm_cache=True, llm_daily_call_limit=2)
    first = LLMClient(settings, Tracer(tmp_path / "traces"))
    first._sleep = lambda _: None
    first._litellm = _fake_litellm([_RateLimited("1"), "ok"])
    messages = [{"role": "user", "content": "first"}]
    assert first.complete("groq/example", messages).text == "ok"
    assert len(first._litellm.calls) == 2
    assert all(call["num_retries"] == 0 for call in first._litellm.calls)
    # A fresh client (as in another worker) shares the persistent quota and cache.
    second = LLMClient(settings, Tracer(tmp_path / "traces"))
    second._litellm = _fake_litellm(["must not call"])
    assert second.complete("groq/example", messages).cached
    second.complete("mock/empty", messages)
    with pytest.raises(RuntimeError, match="Daily provider-call budget"):
        second.complete("groq/example", [{"role": "user", "content": "different"}])
    assert not second._litellm.calls
    engine.dispose()


def test_failed_calls_surface_the_provider_error(tmp_path):
    """LiteLLM retries a failed call through tenacity. Without it, every failure (rate limits included) became
    "tenacity import failed", hiding the provider's error and its 429 status from our own retries."""
    from app.config import Settings

    client = LLMClient(Settings(data_dir=tmp_path, llm_cache=False, ollama_api_base="http://127.0.0.1:9",
                                llm_timeout_s=5, llm_num_retries=1), Tracer(tmp_path / "traces"))
    try:
        client.complete("ollama/none", [{"role": "user", "content": "hi"}])  # nothing listens on port 9
    except Exception as exc:  # noqa: BLE001 - which error it is, is what this test checks
        message = str(exc)
    else:
        pytest.fail("a closed port answered")
    assert "tenacity" not in message


def test_concurrent_calls_are_capped_per_provider(tmp_path):
    from app.config import Settings

    assert Settings(llm_provider_concurrency=" groq = 2 , gemini=1,").provider_concurrency == {"groq": 2, "gemini": 1}
    client = LLMClient(Settings(data_dir=tmp_path, llm_cache=False, llm_provider_concurrency="groq=2"),
                       Tracer(tmp_path / "traces"))
    lock = threading.Lock()
    state = {"active": 0, "peak": 0}

    def completion(**_):
        with lock:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        time.sleep(0.05)
        with lock:
            state["active"] -= 1
        return _reply("ok")
    client._litellm = SimpleNamespace(completion=completion, completion_cost=lambda **_: 0.0)

    def peak(model: str) -> int:
        state["peak"] = 0
        with ThreadPoolExecutor(8) as pool:
            list(pool.map(lambda i: client.complete(model, [{"role": "user", "content": f"{model} {i}"}]), range(8)))
        return state["peak"]
    assert peak("groq/m") == 2
    assert peak("openai/m") > 2  # providers without a limit are not slowed down


def test_cache_roundtrip(tmp_path):
    cache = ResponseCache(tmp_path / "c.sqlite")
    key = cache_key({"a": 1})
    assert cache.get(key) is None
    cache.put(key, {"text": "x"})
    assert cache.get(key) == {"text": "x"}
