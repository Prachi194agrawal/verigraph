"""Score one answer. Which checks run depends on the question family:

  list families      normalized set precision/recall/F1 (+ exact match, invented-item rate, context recall)
  localization       + hit@1 / hit@5 on the ranked list (E5)
  false_premise      + premise_rejected (empty list on a false premise)
  explanations       claim-by-claim verdicts against the graph (+ LLM faithfulness if a judge is set)
  codegen            symbol checks (missing symbols, arity), callee F1 vs the original, optional test run
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from app.config import get_settings
from app.eval.claim_check import Verdict, check_explanation
from app.eval.index import BuildIndex, codegen_language
from app.eval.judge import faithfulness
from app.eval.normalize import normalize_items
from app.eval.sandbox import SandboxResult, run_tests
from app.eval.set_f1 import hit_at_k, set_f1
from app.eval.symbol_check import SymbolUse, check_code, code_metrics
from app.llm.client import LLMClient
from app.pipelines.base import PipelineOutput

SYMBOL_FAMILIES = {"callers", "impact", "inheritance", "localization", "false_premise"}


@dataclass
class QuestionView:
    id: int | None
    family: str
    text: str
    gold_answer: list[str]
    hop_count: int | None = None
    meta: dict = field(default_factory=dict)


@dataclass
class ScoredAnswer:
    items: list[str] = field(default_factory=list)
    scores: dict[str, float] = field(default_factory=dict)
    verdicts: list[Verdict] = field(default_factory=list)
    symbols: list[SymbolUse] = field(default_factory=list)
    test_run: SandboxResult | None = None
    tp: list[str] = field(default_factory=list)
    fp: list[str] = field(default_factory=list)
    fn: list[str] = field(default_factory=list)
    invented: list[str] = field(default_factory=list)  # false positives that do not exist in the repo at all
    notes: list[str] = field(default_factory=list)


class Scorer:
    def __init__(self, index: BuildIndex, llm: LLMClient | None = None, judge_model: str | None = None,
                 run_tests: bool = False, checkout: Path | None = None, build_id: int | None = None) -> None:
        self.index = index
        self.llm = llm
        self.judge_model = judge_model
        self.run_tests = run_tests
        self.checkout = checkout
        self.build_id = build_id

    def score(self, q: QuestionView, out: PipelineOutput, answer_key: str = "answer") -> ScoredAnswer:
        result = ScoredAnswer()
        if out.error:
            result.scores = {"error": 1.0, "f1": 0.0}
            return result
        if q.family == "codegen":
            return self._score_code(q, out, answer_key)

        items = normalize_items(q.family, out.items, self.index, q.gold_answer, q.meta)
        s = set_f1(items, q.gold_answer)
        result.items, result.tp, result.fp, result.fn = items, s.true_positives, s.false_positives, s.false_negatives
        scores = {"precision": s.precision, "recall": s.recall, "f1": s.f1, "exact_match": float(s.f1 == 1.0)}
        if q.family in SYMBOL_FAMILIES and items:
            result.invented = [i for i in s.false_positives if not self.index.is_entity(i)]
            scores["invented_item_rate"] = sum(not self.index.is_entity(i) for i in items) / len(items)
            scores["unrecognized_item_rate"] = scores["invented_item_rate"]
        elif q.family in ("lookup", "files"):
            result.invented = [i for i in s.false_positives if i not in self.index.files]
        if q.family == "localization":
            scores["hit_at_1"] = hit_at_k(items, q.gold_answer, 1)
            scores["hit_at_5"] = hit_at_k(items, q.gold_answer, 5)
        if q.family == "false_premise":
            scores["premise_rejected"] = float(not items)
            scores["premise_true"] = float(bool(q.meta.get("premise")))
        if out.retrieved_nodes and q.gold_answer:
            retrieved = set(normalize_items(q.family, out.retrieved_nodes, self.index, q.gold_answer, q.meta))
            scores["context_recall"] = len(retrieved & set(q.gold_answer)) / len(set(q.gold_answer))

        if out.explanation:
            try:
                result.verdicts = check_explanation(out.explanation, self.index, self.llm, self.judge_model)
            except Exception as exc:  # noqa: BLE001 - an optional judge must not discard deterministic scores
                result.verdicts = check_explanation(out.explanation, self.index)
                scores["judge_error"] = 1.0
                result.notes.append(f"The optional claim extractor failed; rule-based claims were checked. "
                                    f"{type(exc).__name__}: {str(exc)[:300]}")
            labels = [v.label for v in result.verdicts]
            if labels:
                supported, contradicted = labels.count("supported"), labels.count("contradicted")
                scores["claims"] = float(len(labels))
                scores["contradicted_claim_rate"] = contradicted / len(labels)
                scores["unverifiable_claim_rate"] = labels.count("unverifiable") / len(labels)
                if supported + contradicted:
                    scores["claim_precision"] = supported / (supported + contradicted)
            if self.llm is not None and self.judge_model and out.context and not scores.get("judge_error"):
                try:
                    faith = faithfulness(self.llm, self.judge_model, out.context, out.explanation)
                    if faith is not None:
                        scores["faithfulness"] = faith
                except Exception as exc:  # noqa: BLE001
                    scores["judge_error"] = 1.0
                    result.notes.append(f"The optional faithfulness judge failed; this metric was not recorded. "
                                        f"{type(exc).__name__}: {str(exc)[:300]}")
        result.scores = scores
        return result

    def _score_code(self, q: QuestionView, out: PipelineOutput, answer_key: str) -> ScoredAnswer:
        code = out.code or ""
        result = ScoredAnswer()
        original = self.index.functions.get(q.meta.get("blank") or "")
        language = codegen_language(self.index, q.meta)
        if language != "python":
            result.scores = {"unsupported_language": 1.0}
            result.notes = [f"Code-generation syntax, symbol and test checks currently support Python; "
                            f"this {language} answer was not evaluated."]
            result.test_run = SandboxResult("skipped", detail=result.notes[0])
            return result
        if not code.strip():
            result.scores = {"syntax_ok": 0.0, "hallucinated": 0.0, "no_code": 1.0, "callee_f1": 0.0, "f1": 0.0}
            return result
        check = check_code(code, self.index, q.meta.get("module"), q.meta.get("class_qn"),
                           original.kind if original is not None else None)
        result.symbols = check.uses
        scores = code_metrics(check)
        called = sorted(f for f in check.called_functions if f != q.meta.get("blank"))
        callee = set_f1(called, q.gold_answer)
        scores.update({"callee_precision": callee.precision, "callee_recall": callee.recall, "callee_f1": callee.f1,
                       "f1": callee.f1})
        result.notes = ["Static symbol and callee checks do not establish behavioral correctness. "
                        "Run the repository's tests to verify behavior."]
        result.items, result.tp, result.fp, result.fn = called, callee.true_positives, callee.false_positives, \
            callee.false_negatives
        if self.run_tests and self.checkout is not None and self.build_id is not None and check.syntax_ok:
            run = run_tests(self.checkout, self.build_id, q.meta, code, answer_key, get_settings())
            result.test_run = run
            if run.status != "skipped" and run.tests_run:
                scores["test_pass_rate"] = run.passed / run.tests_run
        result.scores = scores
        return result
