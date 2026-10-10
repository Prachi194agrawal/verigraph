"""End-to-end against a real Neo4j: write the fixture graph (with history), generate questions, run the
pipelines with offline mock models, score, and read results through the API. Skipped without Neo4j."""

from __future__ import annotations

import pytest

from app.eval.index import BuildIndex
from app.graph.builder import build_structure
from app.graph.model import Issue
from app.ingest.history import mine_history
from app.llm.client import LLMClient
from app.llm.tracing import Tracer
from app.pipelines import make_pipeline
from app.pipelines.base import PipelineContext, QuestionInput
from app.semantic.embeddings import HashEmbedder, embed_functions
from app.testgen.generator import generate_questions

pytestmark = pytest.mark.neo4j
BUILD = 990_001  # a build id no real build will use


@pytest.fixture(scope="module")
def loaded(store, history_repo):
    g = build_structure(history_repo, "mini", str(history_repo), None, ["jedi", "import", "name"])
    mine_history(history_repo, g, 100)
    # Issue texts normally come from the GitHub API; PR #15's branch commit says "Fixes #3".
    g.issues[3] = Issue(number=3, title="Turbo engine is too slow", body="TurboEngine.run should be faster.")
    store.write_graph(BUILD, g)
    embed_functions(store, BUILD, HashEmbedder(768))
    yield g
    store.delete_build(BUILD)


def test_counts(store, loaded):
    counts = store.counts(BUILD)
    assert counts["nodes"]["Function"] == 20 and counts["nodes"]["Commit"] == 4
    assert counts["edges"]["CALLS"] == 17 and counts["edges"]["INCLUDES"] == 3


def test_gold_answers(store, loaded):
    drafts = generate_questions(store, BUILD, families=["callers", "impact", "inheritance", "history", "lookup"],
                                per_family=50, seed=3)
    by_target = {(d.family, d.target, d.hop_count): d for d in drafts}
    assert sorted(by_target[("callers", "minipkg.utils.helper", 1)].gold_answer) == [
        "minipkg.core.BaseEngine._prepare", "minipkg.utils.compute", "minipkg.utils.normalize"]
    assert by_target[("impact", "minipkg.utils.scale", 1)].gold_answer == ["test_core.test_scale"]
    assert by_target[("impact", "minipkg.utils.scale", 3)].gold_answer == [
        "test_core.TestEngine.test_turbo", "test_core.test_run", "test_core.test_run_reexport", "test_core.test_scale"]
    assert ("impact", "minipkg.utils.scale", 2) not in by_target  # no test is exactly 2 hops away
    assert by_target[("inheritance", "minipkg.core.BaseEngine", 2)].gold_answer == [
        "minipkg.core.Engine", "minipkg.core.TurboEngine"]
    assert by_target[("history", "minipkg.utils.helper", None)].gold_answer == ["Bob", "#12"]
    assert by_target[("history", "minipkg.core.TurboEngine.run", None)].gold_answer == ["Cy", "#15"]
    assert by_target[("lookup", "helper", 1)].gold_answer == ["src/minipkg/utils.py"]


def test_localization_gold_comes_from_the_fixing_pr(store, loaded):
    drafts = generate_questions(store, BUILD, families=["localization"], per_family=5)
    assert len(drafts) == 1
    assert drafts[0].gold_answer == ["minipkg.core.TurboEngine.run"]  # the non-test function PR #15 modified
    assert "Turbo engine is too slow" in drafts[0].text and drafts[0].meta == {"issue": 3}


def test_false_premise_and_codegen(store, loaded):
    drafts = generate_questions(store, BUILD, families=["false_premise", "codegen"], per_family=30, seed=1)
    premises = [d for d in drafts if d.family == "false_premise"]
    assert any(d.gold_answer == [] for d in premises) and any(d.gold_answer for d in premises)
    for d in premises:
        if not d.gold_answer:  # no call path, not even a name edge
            assert not loaded.calls.get((d.target, d.meta["b"]))
    codegen = {d.target: d for d in drafts if d.family == "codegen"}
    assert codegen["minipkg.core.run"].gold_answer == ["minipkg.core.Engine.build", "minipkg.core.Engine.run"]
    assert "test_core.py::test_run" in " ".join(codegen["minipkg.core.run"].meta["tests"])


def test_pipelines_with_mock_models(store, loaded, tmp_path):
    from app.config import Settings
    from app.eval.index import index_rows_from_graph

    llm = LLMClient(Settings(data_dir=tmp_path, llm_cache=False), Tracer(tmp_path / "traces"))
    index = BuildIndex.from_rows(index_rows_from_graph(loaded))
    ctx = PipelineContext(store, llm, index, BUILD, "mini", None, embedding_model="hash")
    q = QuestionInput(1, "callers", "Which non-test functions call `helper` (defined in `src/minipkg/utils.py`) directly?")
    graphrag = make_pipeline("graphrag", ctx).answer(q, "mock/extractive")
    assert sorted(graphrag.items) == ["minipkg.core.BaseEngine._prepare", "minipkg.utils.compute",
                                      "minipkg.utils.normalize"]
    assert "minipkg.utils.compute" in graphrag.retrieved_nodes
    vector = make_pipeline("vector", ctx).answer(q, "mock/extractive")
    assert vector.error is None and vector.retrieved_nodes
    none = make_pipeline("none", ctx).answer(q, "mock/extractive")
    assert none.items == [] and none.retrieved_nodes == []
    blank = QuestionInput(2, "codegen", "Implement `run` in `src/minipkg/core.py`.", {"blank": "minipkg.core.run"})
    ctx_vec = make_pipeline("vector", ctx).retrieve(blank)
    assert "minipkg.core.run" not in ctx_vec.nodes  # the blanked function is never shown
    agent = make_pipeline("cypher_agent", ctx).answer(blank, "mock/extractive")
    assert agent.error and "code-writing" in agent.error


def test_api_flow(store, loaded, tmp_path, monkeypatch):
    """POST /repos -> /builds -> /datasets -> /experiments -> leaderboard, with inline jobs."""
    from fastapi.testclient import TestClient

    from app import db as db_module
    from app.config import get_settings

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'api.db'}")
    monkeypatch.setenv("EMBEDDING_MODEL", "hash")
    get_settings.cache_clear()
    db_module._engine = None
    from app.main import app

    graph_ids: list[int] = []
    try:
        with TestClient(app) as client:
            repo = client.post("/repos", json={"url": str(loaded.repo_url)}).json()
            build = client.post(f"/repos/{repo['id']}/builds", json={"github": False}).json()
            graph_ids.append(build["graph_id"])
            # A fresh database starts at row id 1, but its graph lives under a random Neo4j key.
            assert build["id"] == 1 and build["graph_id"] >= 1_000_000_000
            assert client.get(f"/builds/{build['id']}").json()["status"] == "done"  # background task ran
            quality = client.get(f"/builds/{build['id']}/quality").json()
            assert quality["counts"]["edges"]["CALLS"] == 17 and quality["graph_id"] == build["graph_id"]
            assert store.counts(build["graph_id"])["edges"]["CALLS"] == 17
            hits = client.get(f"/builds/{build['id']}/symbols", params={"q": "helper"}).json()
            assert any(h["qualified_name"] == "minipkg.utils.helper" for h in hits)
            nb = client.get(f"/builds/{build['id']}/neighborhood", params={"qn": "minipkg.utils.helper"}).json()
            assert {"minipkg.utils.normalize", "minipkg.utils.compute"} <= {n["id"] for n in nb["nodes"]}
            ds = client.post("/datasets", json={"build_id": build["id"], "per_family": 3,
                                                "families": ["callers", "impact"]}).json()
            assert client.get(f"/datasets/{ds['id']}").json()["n_questions"] >= 3
            exps = client.post("/experiments", json={"dataset_id": ds["id"], "models": ["mock/extractive"],
                                                     "pipelines": ["none", "graphrag"], "concurrency": 2}).json()
            board = client.get("/leaderboard", params={"dataset_id": ds["id"]}).json()
            assert {r["pipeline"] for r in board} == {"none", "graphrag"}
            best = board[0]
            assert best["pipeline"] == "graphrag" and best["f1"] > 0
            results = client.get(f"/experiments/{exps[1]['id']}/results").json()
            checks = client.get(f"/answers/{results['answers'][0]['answer_id']}/checks").json()
            assert checks["question"]["gold_answer"] and "scores" in checks
            assert checks["build_id"] == build["id"]  # the dashboard links into the explorer by row id
            assert checks["question"]["build_params"]["build_id"] == build["graph_id"]  # gold Cypher uses the key

            # A redelivered dataset job (Celery acks_late after a worker crash) must not duplicate questions.
            from app.workers.jobs import run_dataset
            n = client.get(f"/datasets/{ds['id']}").json()["n_questions"]
            run_dataset(ds["id"])
            assert len(client.get(f"/datasets/{ds['id']}/questions").json()) == n

            # Developer questions: any pipeline, and the answer's claims are checked against the graph.
            asked = client.post(f"/builds/{build['id']}/ask", json={
                "question": "Which functions call `helper` directly?", "model": "mock/extractive"}).json()
            assert {i["name"] for i in asked["answer"]} == {
                "minipkg.core.BaseEngine._prepare", "minipkg.utils.compute", "minipkg.utils.normalize"}
            assert all(i["in_graph"] for i in asked["answer"]) and asked["summary"]["supported"] == 3
            assert client.post(f"/builds/{build['id']}/ask", json={
                "question": "x?", "model": "m", "pipeline": "nope"}).status_code == 422

            impact = client.post(f"/builds/{build['id']}/impact", json={"functions": ["utils.scale"], "max_hops": 1})
            assert impact.json()["tests"][0]["pytest_id"] == "tests/test_core.py::test_scale"
            assert "Tests to run (1)" in impact.json()["markdown"]
            assert client.post(f"/builds/{build['id']}/impact", json={}).status_code == 422
            assert client.get(f"/builds/{build['id']}/quality").json()["symbol_audit"]["functions_flagged"] == 0

            deleted = client.delete(f"/builds/{build['id']}").json()
            assert deleted["datasets"] == 1 and deleted["experiments"] == 2 and deleted["answers"] > 0
            assert client.get(f"/builds/{build['id']}").status_code == 404
            assert client.get(f"/datasets/{ds['id']}").status_code == 404
            assert store.counts(build["graph_id"])["nodes"] == {}
    finally:  # a failed assertion must not leave this build in Neo4j for the next run
        for graph_id in graph_ids:
            store.delete_build(graph_id)
        get_settings.cache_clear()
        db_module._engine = None


def test_failed_model_calls_are_retried_not_scored_as_wrong(store, loaded, tmp_path, monkeypatch):
    """A failed call (rate limit, timeout) is not a wrong answer: the finished experiment stays resumable, and
    resuming asks only those questions again, ending with the scores of a run whose calls never failed."""
    import json

    from fastapi.testclient import TestClient

    from app import db as db_module
    from app.config import get_settings
    from app.db import session_scope
    from app.llm import client as client_module
    from app.llm.client import get_llm
    from app.models import Dataset, Experiment, GraphBuild, Repo
    from app.workers.jobs import run_dataset, run_experiment

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'retry.db'}")
    monkeypatch.setenv("EMBEDDING_MODEL", "hash")
    monkeypatch.setenv("LLM_CACHE", "false")  # every retry must reach the model
    get_settings.cache_clear()
    get_llm.cache_clear()
    db_module._engine = None
    from app.main import app

    def new_experiment() -> int:
        with session_scope() as db:
            exp = Experiment(dataset_id=dataset_id, model="mock/extractive", pipeline="graphrag", prompt_version="v1",
                             options={"concurrency": 1})
            db.add(exp)
            db.flush()
            return exp.id

    try:
        with TestClient(app) as client:
            with session_scope() as db:  # the fixture graph that `loaded` wrote, registered as a finished build
                repo = Repo(url=str(loaded.repo_url), name="mini")
                db.add(repo)
                db.flush()
                build = GraphBuild(repo_id=repo.id, repo="mini", graph_id=BUILD, parser_version="test", status="done",
                                   stats={"embeddings": {"embedding_model": "hash"}})
                db.add(build)
                db.flush()
                ds = Dataset(build_id=build.id, version="retry", options={"families": ["callers", "impact"]})
                db.add(ds)
                db.flush()
                dataset_id = ds.id
            run_dataset(dataset_id)
            clean = new_experiment()
            run_experiment(clean)
            want = client.get(f"/experiments/{clean}/results").json()

            seen: set[str] = set()
            real = client_module.mock_complete

            def flaky(model, messages):  # every other new prompt fails once, as a rate limit would
                key = json.dumps(messages)
                first = key not in seen
                seen.add(key)
                if first and len(seen) % 2:
                    raise RuntimeError("RateLimitError: simulated HTTP 429")
                return real(model, messages)
            monkeypatch.setattr(client_module, "mock_complete", flaky)

            exp_id = new_experiment()
            run_experiment(exp_id)
            first = client.get(f"/experiments/{exp_id}/results").json()
            failed = [a for a in first["answers"] if a["error"]]
            assert first["experiment"]["status"] == "done" and 0 < len(failed) < len(first["answers"])
            assert first["summary"]["errors"] == len(failed) and first["summary"]["f1"] <= want["summary"]["f1"]
            assert first["summary"]["f1_answered"] is not None
            compared = client.get("/compare", params={"a": exp_id, "b": clean}).json()
            assert compared["excluded_failed_calls"] == len(failed) and compared["diff"] == 0.0

            assert client.post(f"/experiments/{clean}/resume").status_code == 409  # nothing failed there
            assert client.post(f"/experiments/{exp_id}/resume").status_code == 202  # runs inline
            after = client.get(f"/experiments/{exp_id}/results").json()
            assert after["experiment"]["status"] == "done" and after["summary"]["errors"] == 0
            assert sorted(a["question_id"] for a in after["answers"]) == sorted(a["question_id"] for a in want["answers"])
            assert after["summary"]["f1"] == want["summary"]["f1"]
    finally:
        get_settings.cache_clear()
        get_llm.cache_clear()
        db_module._engine = None


def test_a_failing_semantic_model_does_not_fail_the_build(store, history_repo, tmp_path, monkeypatch):
    """Summaries and feature tags are optional and never gold: a provider error there is recorded on the build,
    and the structure graph it already wrote stays usable."""
    from app import db as db_module
    from app.config import get_settings
    from app.db import init_db, session_scope
    from app.models import GraphBuild, Repo
    from app.workers import jobs

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'semantic.db'}")
    monkeypatch.setenv("EMBEDDING_MODEL", "hash")
    get_settings.cache_clear()
    db_module._engine = None

    def used_up(*_args, **_kwargs):
        raise RuntimeError("RateLimitError: daily token quota used up")
    monkeypatch.setattr(jobs, "summarize_functions", used_up)
    graph_id = None
    try:
        init_db()
        with session_scope() as db:
            repo = Repo(url=str(history_repo), name="mini")
            db.add(repo)
            db.flush()
            build = GraphBuild(repo_id=repo.id, repo="mini", parser_version="test",
                               options={"github": False, "semantic_model": "mock/empty"})
            db.add(build)
            db.flush()
            build_id, graph_id = build.id, build.graph_id
        jobs.run_build(build_id)
        with session_scope() as db:
            build = db.get(GraphBuild, build_id)
            assert build.status == "done", build.error
            assert "daily token quota" in build.stats["semantic_error"]
        assert store.counts(graph_id)["edges"]["CALLS"] == 17
    finally:
        if graph_id is not None:
            store.delete_build(graph_id)
        get_settings.cache_clear()
        db_module._engine = None


def test_agent_that_never_answers_is_scored_not_retried(store, loaded, tmp_path, monkeypatch):
    """Running out of turns is the model's failure: an empty answer, not a failed call to ask again."""
    from app.config import Settings
    from app.eval.index import index_rows_from_graph
    from app.llm import client as client_module

    monkeypatch.setattr(client_module, "mock_complete", lambda model, messages: '{"action": "query", "cypher": "RETURN 1 AS x"}')
    llm = LLMClient(Settings(data_dir=tmp_path, llm_cache=False), Tracer(tmp_path / "traces"))
    ctx = PipelineContext(store, llm, BuildIndex.from_rows(index_rows_from_graph(loaded)), BUILD, "mini", None)
    out = make_pipeline("cypher_agent", ctx).answer(QuestionInput(1, "callers", "Who calls `helper`?"), "mock/extractive")
    assert out.error is None and out.items == [] and len(out.steps) == 4


def test_graphrag_context_has_no_duplicate_edges(store, loaded, tmp_path):
    from app.config import Settings
    from app.eval.index import index_rows_from_graph

    llm = LLMClient(Settings(data_dir=tmp_path, llm_cache=False), Tracer(tmp_path / "traces"))
    ctx = PipelineContext(store, llm, BuildIndex.from_rows(index_rows_from_graph(loaded)), BUILD, "mini", None,
                          options={"graphrag_hops": 3})
    retrieval = make_pipeline("graphrag", ctx).retrieve(QuestionInput(1, "impact", "If `helper` changes, which tests fail?"))
    edge_lines = [line for line in retrieval.context.splitlines() if " -" in line and "-> " in line]
    assert edge_lines and len(edge_lines) == len(set(edge_lines))
    assert "minipkg.core.run -CALLS-> minipkg.core.Engine.run" in "\n".join(edge_lines)  # hop 3 back from helper


def test_write_graph_refuses_to_overwrite_another_repos_build(store, loaded):
    from app.graph.model import CodeGraph

    with pytest.raises(RuntimeError, match="already holds build"):
        store.write_graph(BUILD, CodeGraph(repo_name="other", repo_url="https://example.com/other", commit_sha=None))
    assert store.counts(BUILD)["nodes"]["Function"] == 20  # untouched


def test_semantic_layer_and_llm_judges_with_mock(store, loaded, tmp_path):
    from app.config import Settings
    from app.eval.judge import faithfulness, judge_semantic
    from app.semantic.features import tag_features
    from app.semantic.summaries import summarize_functions
    from app.testgen.rephraser import rephrase

    llm = LLMClient(Settings(data_dir=tmp_path, llm_cache=False), Tracer(tmp_path / "traces"))
    stats = summarize_functions(store, BUILD, llm, "mock/empty")
    library = [f for f in loaded.functions.values() if not loaded.files[f.file_path].is_test]
    assert stats["summarized_functions"] == len(library)  # test-file functions are skipped
    rows = store.read("MATCH (f:Function {build_id: $b}) WHERE f.summary IS NOT NULL RETURN count(f) AS n", {"b": BUILD})
    assert rows[0]["n"] == len(library)
    feats = tag_features(store, BUILD, llm, "mock/empty", "mini")
    assert feats["features"] == 1 and feats["implements_edges"] >= 1
    conf = store.read("MATCH (:Function {build_id: $b})-[r:IMPLEMENTS]->(:Feature) RETURN max(r.confidence) AS c",
                      {"b": BUILD})[0]["c"]
    assert conf <= 0.95  # LLM edges never claim certainty
    assert rephrase(llm, "mock/empty", "Which functions call `helper`?") == "Quick question: Which functions call `helper`?"
    assert rephrase(llm, "mock/empty", "Which file defines `x`?").endswith("`x`?")
    assert faithfulness(llm, "mock/empty", "context", "explanation") == 1.0
    assert judge_semantic(llm, "mock/empty", "mini", "def f(): pass", "summary", "Does nothing.") == "faithful"


def test_vector_search_survives_other_builds_crowding_the_index(store, loaded):
    """The vector index is shared by all builds. 300 functions of another build that sit exactly on the query
    vector fill the over-fetched neighbours; this build must still get its k results."""
    from app.semantic.embeddings import HashEmbedder

    other = BUILD + 1
    query = HashEmbedder(768).embed_queries(["helper normalize scale"])[0].tolist()
    store.write("UNWIND range(1, 300) AS i CREATE (:Function {build_id: $b, uid: $b + ':fn:x' + i, "
                "qualified_name: 'crowd.f' + i, code_embedding: $vec})", {"b": other, "vec": query})
    try:
        rows = store.vector_search(BUILD, query, k=5)
        assert len(rows) == 5
        assert all(r["qualified_name"].startswith(("minipkg.", "test_core")) for r in rows)
        assert rows == sorted(rows, key=lambda r: -r["score"])
    finally:
        store.delete_build(other)
