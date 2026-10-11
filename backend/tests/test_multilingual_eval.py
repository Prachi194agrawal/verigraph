"""Evaluation checks must describe what is known instead of treating partial graphs as runtime truth."""

from types import SimpleNamespace

from app.analysis.ask import BuildRef, ask
from app.analysis.symbol_audit import symbol_self_audit
from app.config import Settings
from app.eval.claim_check import check_explanation, extract_claims_rules
from app.eval.index import BuildIndex
from app.eval.judge import faithfulness
from app.eval.normalize import normalize_items
from app.eval.sandbox import run_tests
from app.eval.scorer import QuestionView, Scorer
from app.llm.client import LLMClient
from app.llm.prompts import render
from app.llm.tracing import Tracer
from app.pipelines.base import Pipeline, PipelineContext, PipelineOutput, QuestionInput
from app.pipelines.context import code_chunk
from app.pipelines.cypher_agent import CypherAgentPipeline
from app.pipelines.graphrag import GraphRAGPipeline
from app.pipelines.vector import VectorPipeline
from app.testgen.generator import generate_questions


def js_index():
    functions = [{"qualified_name": f"javascript:src/app.js.{name}", "name": name, "module": "javascript:src/app.js",
                  "file_path": "src/app.js", "language": "javascript"} for name in ("run", "helper")]
    return BuildIndex(functions, [], [{"path": "src/app.js", "module": "javascript:src/app.js",
                                      "language": "javascript", "analysis_level": "structural"}],
                      calls=[{"caller": "javascript:src/app.js.run", "callee": "javascript:src/app.js.helper",
                              "resolved_by": "static", "line": 2}])


def test_multilingual_static_edge_is_supported_and_missing_calls_are_unknown():
    verdicts = check_explanation("`run` calls `helper`. `helper` calls `run`. `run` does not call `helper`.",
                                js_index())
    assert [(v.claim.relation, v.label) for v in verdicts] == [
        ("calls", "supported"), ("calls", "unverifiable"), ("not_calls", "contradicted")]
    assert "runtime" in verdicts[1].evidence


def test_missing_test_paths_and_inheritance_do_not_prove_contradiction(mini_index):
    text = "`test_scale` tests `helper`. `BaseEngine` inherits from `TurboEngine`."
    assert [v.label for v in check_explanation(text, mini_index)] == ["unverifiable", "unverifiable"]


def test_multilingual_paths_and_code_fences():
    index = js_index()
    assert normalize_items("callers", ["src/app.js::run"], index, []) == ["javascript:src/app.js.run"]
    claims = extract_claims_rules("`run` is defined in src/app.js.")
    assert claims[0].object == "src/app.js"
    assert check_explanation("`run` is defined in src/app.js.", index)[0].label == "supported"
    assert "```javascript\n" in code_chunk("run", "src/app.js", 1, 3, "function run() {}")
    assert "```typescript\n" in code_chunk("run", "src/app.ts", 1, 3, "function run() {}")
    assert "```text\n" in code_chunk("run", "src/app.unknown", 1, 3, "something")


def test_non_python_codegen_is_not_graded_as_invalid_python_or_executed(tmp_path, monkeypatch):
    q = QuestionView(1, "codegen", "Implement run", [],
                     meta={"blank": "javascript:src/app.js.run", "file": "src/app.js", "language": "python"})
    result = Scorer(js_index(), run_tests=True, checkout=tmp_path, build_id=1).score(
        q, PipelineOutput(code="function run() { return helper(); }"))
    assert result.scores == {"unsupported_language": 1.0}
    assert result.test_run.status == "skipped" and result.notes
    monkeypatch.setattr("app.eval.sandbox.shutil.which", lambda _name: (_ for _ in ()).throw(
        AssertionError("non-Python execution must be skipped before checking Docker")))
    run = run_tests(tmp_path, 1, {"file": "src/app.js", "tests": ["test"]}, "function run() {}", "x",
                    Settings(data_dir=tmp_path))
    assert run.status == "skipped" and "Python" in run.detail


def test_non_python_codegen_pipeline_skips_provider_call():
    tracer = SimpleNamespace(new_trace_id=lambda: "test")
    ctx = PipelineContext(None, SimpleNamespace(tracer=tracer), js_index(), 1, "test", None)
    out = Pipeline(ctx).answer(QuestionInput(1, "codegen", "Implement run", {"file": "src/app.js"}), "real/model")
    assert "not supported" in out.error and out.prompt_tokens == 0


def test_symbol_self_audit_skips_non_python_source():
    class Store:
        def read(self, *_args):
            return [{"qn": "javascript:src/app.js.run", "module": "javascript:src/app.js", "class_qn": None,
                     "code": "function run() { return helper(); }"}]

    audit = symbol_self_audit(Store(), 1, js_index())
    assert audit["functions_checked"] == 0 and audit["functions_skipped_other_languages"] == 1
    assert audit["false_positive_rate"] is None


def test_free_form_answers_report_scope_mock_and_lexical_limitations(tmp_path):
    llm = LLMClient(Settings(data_dir=tmp_path, llm_cache=False), Tracer(tmp_path / "traces"))
    result = ask(None, llm, js_index(), BuildRef(1, 1, "example", "abc", "hash"),
                 "Which functions call helper?", "mock/empty", pipeline="none")
    assert result["error"] is None
    assert result["provenance"] == {"commit_sha": "abc", "reference_source": "static_code_graph",
                                   "model_kind": "mock", "embedding_model": "hash", "languages": ["javascript"]}
    assert any("test fixture" in note for note in result["limitations"])
    assert any("lexical" in note for note in result["limitations"])
    assert any("no reference answer" in note for note in result["limitations"])


def test_multilingual_v2_prompts_keep_v1_available():
    prompt = render("answer_system", "v2", repo="r", commit="abc")
    assert "Python repository" not in prompt and "runtime" in prompt
    assert "Python repository" in render("answer_system", "v1", repo="r", commit="abc")
    agent = render("cypher_agent", "v2", repo="r", commit="abc", schema="S", max_rows=5, max_queries=2)
    assert '"static"' in agent and "server enforces" in agent


def test_file_inventory_only_language_can_generate_graph_grounded_questions_and_answers(tmp_path):
    index = BuildIndex([], [], [{"path": "lib/main.dart", "module": "dart:lib/main.dart", "language": "dart",
                                 "analysis_level": "file"}])

    class Store:
        def read(self, cypher, params):
            assert params["build_id"] == 7
            if "DISTINCT f.language" in cypher:
                return [{"language": "dart"}]
            assert params["language"] == "dart" and params["limit"] == 10
            return [{"gold": "lib/main.dart"}]

    drafts = generate_questions(Store(), 7, families=["files"], per_family=5)
    assert len(drafts) == 1 and drafts[0].gold_answer == ["lib/main.dart"]
    assert drafts[0].meta["coverage"] == "file_inventory"
    llm = LLMClient(Settings(data_dir=tmp_path, llm_cache=False), Tracer(tmp_path / "traces"))
    ctx = PipelineContext(Store(), llm, index, 7, "dart-repo", "abc")
    q = QuestionInput(1, "files", drafts[0].text, drafts[0].meta)
    out = GraphRAGPipeline(ctx).answer(q, "mock/extractive")
    assert out.error is None and out.items == ["lib/main.dart"]
    score = Scorer(index).score(QuestionView(1, "files", q.text, drafts[0].gold_answer), out)
    assert score.scores["f1"] == 1.0


def test_file_inventory_ask_never_claims_function_support(tmp_path):
    index = BuildIndex([], [], [{"path": "src/View.vue", "module": "vue:src/View.vue", "language": "vue",
                                 "analysis_level": "file"}])
    llm = LLMClient(Settings(data_dir=tmp_path, llm_cache=False), Tracer(tmp_path / "traces"))
    result = ask(None, llm, index, BuildRef(1, 1, "example", "abc", "hash"),
                 "Which `vue` files were indexed?", "mock/extractive")
    assert result["error"] is None and result["answer"][0]["name"] == "src/View.vue"
    assert result["answer"][0]["kind"] == "file"
    assert any("without function relationships" in note for note in result["limitations"])


def test_vector_pipeline_without_functions_avoids_loading_embeddings():
    index = BuildIndex([], [], [{"path": "lib/main.dart", "module": "dart:lib/main.dart", "language": "dart",
                                 "analysis_level": "file"}])
    ctx = PipelineContext(None, None, index, 1, "example", None)
    retrieval = VectorPipeline(ctx).retrieve(QuestionInput(1, "files", "List Dart files", {"language": "dart"}))
    assert retrieval.nodes == [] and "file inventory only" in retrieval.context


def test_history_author_index_does_not_prove_who_modified_a_function_last():
    index = js_index()
    index.modified_by["javascript:src/app.js.run"] = {"ada", "bob"}
    verdicts = check_explanation("`run` was changed by Ada. `run` was last changed by Bob.", index)
    assert [v.label for v in verdicts] == ["supported", "unverifiable"]


def test_optional_judge_failure_preserves_graph_scores_and_rule_evidence():
    class Judge:
        def complete_json(self, *_args, **_kwargs):
            raise RuntimeError("judge quota exhausted")

    scorer = Scorer(js_index(), llm=Judge(), judge_model="example/judge")
    out = PipelineOutput(items=["helper"], explanation="`run` calls `helper`.", context="known call graph")
    result = scorer.score(QuestionView(1, "callers", "?", ["javascript:src/app.js.helper"]), out)
    assert result.scores["f1"] == 1.0 and result.scores["judge_error"] == 1.0
    assert result.verdicts[0].label == "supported" and result.notes
    assert "faithfulness" not in result.scores


def test_faithfulness_judge_does_not_treat_the_string_false_as_true():
    class Judge:
        def complete_json(self, *_args, **_kwargs):
            return {"statements": [{"supported": "false"}, {"supported": True}, {"supported": False}, {}]}, None

    assert faithfulness(Judge(), "example/judge", "context", "explanation") == 0.5


def test_cypher_agent_always_uses_enforced_build_scoping():
    class Store:
        def read(self, *_args, **_kwargs):
            raise AssertionError("agent queries cannot use unrestricted reads")

        def read_scoped(self, cypher, build_id, timeout):
            assert cypher == "MATCH (f:Function) RETURN f.qualified_name AS name"
            assert build_id == 42 and timeout == 15
            return [{"name": "javascript:src/app.js.run"}]

    class LLM:
        tracer = SimpleNamespace(new_trace_id=lambda: "test", log=lambda *_args: None)
        responses = iter(['{"action":"query","cypher":"MATCH (f:Function) RETURN f.qualified_name AS name"}',
                          '{"action":"answer","answer":["javascript:src/app.js.run"],"explanation":"Recorded symbol."}'])

        def complete(self, *_args, **_kwargs):
            return SimpleNamespace(text=next(self.responses), prompt_tokens=1, completion_tokens=1,
                                   cost_usd=0.0, latency_ms=0.1)

    ctx = PipelineContext(Store(), LLM(), js_index(), 42, "example", None)
    out = CypherAgentPipeline(ctx).answer(QuestionInput(1, "callers", "List functions"), "example/model")
    assert out.error is None and out.items == ["javascript:src/app.js.run"]
    assert out.steps[0]["rows"] == 1 and out.steps[0]["error"] is None
