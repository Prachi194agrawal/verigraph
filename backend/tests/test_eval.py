import pytest

from app.eval.claim_check import check_explanation, extract_claims_rules
from app.eval.meta_eval import bootstrap_ci, cohens_kappa, edge_precision, paired_bootstrap
from app.eval.normalize import normalize_items
from app.eval.sandbox import replace_function
from app.eval.scorer import QuestionView, Scorer
from app.eval.set_f1 import hit_at_k, set_f1
from app.eval.symbol_check import check_arity, check_code, code_metrics
from app.graph.model import Param
from app.pipelines.base import PipelineOutput


def test_set_f1_edge_cases():
    assert set_f1([], []).f1 == 1.0  # correctly rejected false premise
    assert set_f1([], ["a"]).f1 == 0.0
    assert set_f1(["a"], []).f1 == 0.0
    s = set_f1(["a", "b", "x"], ["a", "b", "c", "d"])
    assert (round(s.precision, 3), s.recall, round(s.f1, 3)) == (0.667, 0.5, 0.571)
    assert s.false_positives == ["x"] and s.false_negatives == ["c", "d"]
    assert hit_at_k(["x", "a"], ["a"], 1) == 0.0 and hit_at_k(["x", "a"], ["a"], 5) == 1.0


def test_normalize_symbols_paths_and_history(mini_index):
    gold = ["minipkg.utils.compute", "minipkg.utils.normalize"]
    items = normalize_items("callers", ["compute()", "`minipkg.utils.normalize`", "def compute",
                                        "src/minipkg/core.py::BaseEngine::_prepare", "made.up"], mini_index, gold)
    assert items == ["minipkg.utils.compute", "minipkg.utils.normalize", "minipkg.core.BaseEngine._prepare",
                     "made.up"]
    # ambiguous suffix ("run" matches 3 functions) resolves only through the gold set
    assert normalize_items("callers", ["Engine.run", "run"], mini_index, ["minipkg.core.run"]) == [
        "minipkg.core.Engine.run", "minipkg.core.run"]
    assert normalize_items("lookup", ["minipkg/utils.py", "./src/minipkg/core.py", "minipkg.core"], mini_index,
                           ["src/minipkg/utils.py"]) == ["src/minipkg/utils.py", "src/minipkg/core.py"]
    assert normalize_items("history", ["Bob in PR #12", "ADA", "15"], mini_index, [],
                           {"aliases": {"Ada": ["ada@example.com"]}}) == ["#12", "Bob", "Ada", "#15"]


def test_claim_extraction_rules():
    claims = {(c.subject, c.relation, c.object) for c in extract_claims_rules(
        "`a.f` calls `b.g` and `c.h`. `x.y` is called by `z.w`. `A` inherits from `B`. "
        "`f` is defined in pkg/mod.py. `a.f` does not call `q.r`. utils.scale was last changed by Ada Lovelace.")}
    assert claims == {("a.f", "calls", "b.g"), ("a.f", "calls", "c.h"), ("z.w", "calls", "x.y"),
                      ("A", "inherits", "B"), ("f", "defined_in", "pkg/mod.py"), ("a.f", "not_calls", "q.r"),
                      ("utils.scale", "modified_by", "Ada Lovelace")}


def test_claim_verdicts(mini_index):
    verdicts = {(v.claim.subject, v.claim.relation, v.claim.object): v.label for v in check_explanation(
        "`Engine.run` calls `normalize`. `normalize` calls `scale`. `TurboEngine` inherits from `BaseEngine`. "
        "`helper` is defined in src/minipkg/core.py. `dispatch` does not call `helper`. `run` calls `helper`.",
        mini_index)}
    assert verdicts[("Engine.run", "calls", "normalize")] == "supported"
    assert verdicts[("normalize", "calls", "scale")] == "unverifiable"
    assert verdicts[("TurboEngine", "inherits", "BaseEngine")] == "supported"
    assert verdicts[("helper", "defined_in", "src/minipkg/core.py")] == "contradicted"
    assert verdicts[("dispatch", "not_calls", "helper")] == "unverifiable"
    assert verdicts[("run", "calls", "helper")] == "unverifiable"  # "run" is ambiguous


def test_check_arity():
    params = [Param("self", "positional"), Param("a", "positional"), Param("b", "positional", True),
              Param("k", "keyword_only")]
    assert check_arity(params, 1, ["k"], drop_first=True)[0]
    assert check_arity(params, 3, ["k"], drop_first=True)[1] == "wrong_arity"
    assert check_arity(params, 1, ["k", "zz"], drop_first=True)[1] == "unknown_keyword"
    assert "missing" in check_arity(params, 1, [], drop_first=True)[2]
    assert "multiple values" in check_arity(params, 1, ["a", "k"], drop_first=True)[2]


def test_symbol_checker_categories(mini_index):
    code = (
        "def run(self, x):\n"
        "    from .utils import scale, magic\n"
        "    y = norm(x, 3)\n"
        "    e = Engine('n')\n"
        "    e.missing_method()\n"
        "    self.start()\n"
        "    utils.scale(y, factor=2, extra=1)\n"
        "    return Session() + undefined_thing\n")
    check = check_code(code, mini_index, module="minipkg.core", class_qn="minipkg.core.Engine")
    found = {(u.symbol, u.category) for u in check.problems}
    assert ("minipkg.utils.magic", "missing_symbol") in found
    assert ("norm", "wrong_arity") in found
    assert ("minipkg.core.Engine.missing_method", "missing_symbol") in found
    assert ("utils.scale", "unknown_keyword") in found
    assert ("Session", "undefined_name") in found and ("undefined_thing", "undefined_name") in found
    assert {"minipkg.core.BaseEngine.start", "minipkg.core.Engine.__init__"} <= check.called_functions
    metrics = code_metrics(check)
    assert metrics["hallucinated"] == 1.0 and metrics["syntax_ok"] == 1.0
    assert not check_code("def broken(:\n", mini_index).syntax_ok


def test_symbol_checker_clean_code_and_missing_import(mini_index):
    clean = check_code("def run(x):\n    engine = Engine.build('d')\n    return engine.run(x) + helper(1)\n",
                       mini_index, module="minipkg.core")
    assert not clean.problems
    missing = check_code("def f():\n    return scale(1)\n", mini_index, module="minipkg.core")
    assert [(u.symbol, u.category) for u in missing.problems] == [("scale", "missing_import")]


def test_scorer_families(mini_index):
    scorer = Scorer(mini_index)
    q = QuestionView(1, "callers", "?", ["minipkg.core.run"])
    out = PipelineOutput(items=["run", "minipkg.utils.ghost"], explanation="`minipkg.core.run` calls `Engine.build`.",
                         retrieved_nodes=["minipkg.core.run"])
    scored = scorer.score(q, out)
    assert scored.scores["f1"] == pytest.approx(2 / 3) and scored.scores["invented_item_rate"] == 0.5
    assert scored.scores["context_recall"] == 1.0 and scored.scores["claim_precision"] == 1.0
    premise = scorer.score(QuestionView(2, "false_premise", "?", [], meta={"premise": False}), PipelineOutput(items=[]))
    assert premise.scores["f1"] == 1.0 and premise.scores["premise_rejected"] == 1.0
    code_q = QuestionView(3, "codegen", "?", ["minipkg.core.Engine.build", "minipkg.core.Engine.run"],
                          meta={"module": "minipkg.core", "blank": "minipkg.core.run"})
    code = scorer.score(code_q, PipelineOutput(code="def run(x):\n    return Engine.build('d').run(x)\n"))
    assert code.scores["callee_f1"] == 1.0 and code.scores["hallucinated"] == 0.0
    assert scorer.score(q, PipelineOutput(error="timeout")).scores["error"] == 1.0


def test_meta_eval():
    assert cohens_kappa(["a", "a", "b", "b"], ["a", "a", "b", "b"]) == 1.0
    assert cohens_kappa(["a", "b", "a", "b"], ["b", "a", "b", "a"]) == -1.0
    assert cohens_kappa(["a"], ["a"]) is None
    ci = bootstrap_ci([0.0, 1.0] * 50)
    assert ci["mean"] == 0.5 and ci["lo"] < 0.5 < ci["hi"]
    diff = paired_bootstrap({i: 1.0 for i in range(30)}, {i: 0.0 for i in range(30)})
    assert diff["diff"] == 1.0 and diff["p"] == 0.0
    assert edge_precision([{"resolved_by": "jedi", "label": "correct"}, {"resolved_by": "jedi", "label": "incorrect"},
                           {"resolved_by": "name", "label": ""}]) == {
        "jedi": {"checked": 2, "correct": 1, "precision": 0.5}, "name": {"checked": 0, "correct": 0, "precision": None}}


def test_replace_function_keeps_decorators_and_indent(tmp_path):
    f = tmp_path / "m.py"
    f.write_text("class A:\n    @staticmethod\n    def f(x):\n        return x\n\n    def g(self):\n        pass\n")
    replace_function(f, 3, 4, "@staticmethod\ndef f(x):\n    return x * 2\n")
    assert f.read_text() == ("class A:\n    @staticmethod\n    def f(x):\n        return x * 2\n\n"
                             "    def g(self):\n        pass\n")


def test_symbol_checker_has_no_false_positives_on_valid_code(mini_index):
    def problems(code, class_qn="minipkg.core.BaseEngine", kind=None):
        check = check_code(code, mini_index, module="minipkg.core", class_qn=class_qn, kind=kind)
        return [(u.symbol, u.category) for u in check.uses if u.category]

    # A base class (or mixin) may use attributes that its subclasses provide: self can be an Engine.
    assert problems("def start(self):\n    return self.name\n") == []
    # ...but an attribute that no class in the hierarchy has is still an invented API.
    assert problems("def start(self):\n    return self.speed\n") == [("minipkg.core.BaseEngine.speed", "missing_symbol")]
    # Python's own object attributes always exist.
    assert problems("def start(self):\n    return self.__dict__, self.__class__.__name__\n") == []
    # Engine.build is a staticmethod: its first parameter is not self, even when the model omits the decorator.
    code = "def build(name):\n    return name.upper()\n"
    assert problems(code, "minipkg.core.Engine", kind="staticmethod") == []
    assert problems(code, "minipkg.core.Engine") != []  # without the kind, `name` would be typed as an Engine


def test_hop_curve_uses_impact_questions_only():
    from app.eval.report import summarize

    def row(family, hops, f1):
        return {"family": family, "hop_count": hops, "premise": None, "latency_ms": 1.0, "cost_usd": 0.0,
                "tokens": 0, "error": None, "scores": {"f1": f1}}

    s = summarize([row("impact", 1, 0.5), row("impact", 2, 0.0), row("inheritance", 1, 1.0)])
    assert s["f1_by_hop"] == {"1": 0.5, "2": 0.0}  # the easy inheritance question must not lift hop 1
    assert s["f1_by_inheritance_depth"] == {"1": 1.0}
