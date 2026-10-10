import json

from app.graph.store import is_read_only
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


def test_cache_roundtrip(tmp_path):
    cache = ResponseCache(tmp_path / "c.sqlite")
    key = cache_key({"a": 1})
    assert cache.get(key) is None
    cache.put(key, {"text": "x"})
    assert cache.get(key) == {"text": "x"}
