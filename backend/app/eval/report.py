"""Aggregate per-answer scores into the leaderboard, the F1-by-hop curve and paired comparisons."""

from __future__ import annotations

from collections import defaultdict
from statistics import mean, median

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.eval.meta_eval import bootstrap_ci, paired_bootstrap
from app.models import Answer, Experiment, Question, Score

CODEGEN_METRICS = ("syntax_ok", "hallucinated", "missing_symbol_rate", "wrong_arity_rate", "callee_f1",
                   "test_pass_rate")
EXTRA_METRICS = ("context_recall", "claim_precision", "contradicted_claim_rate", "unverifiable_claim_rate", "invented_item_rate",
                 "unrecognized_item_rate", "faithfulness", "judge_error", "hit_at_1", "hit_at_5", "premise_rejected")


def answer_rows(db: Session, experiment_id: int) -> list[dict]:
    """One dict per answer: question fields, cost/latency and all scores."""
    answers = db.execute(
        select(Answer, Question).join(Question, Answer.question_id == Question.id)
        .where(Answer.experiment_id == experiment_id).order_by(Answer.id)).all()
    scores: dict[int, dict[str, float]] = defaultdict(dict)
    ids = [a.id for a, _ in answers]
    for chunk in (ids[i:i + 900] for i in range(0, len(ids), 900)):
        for s in db.execute(select(Score).where(Score.answer_id.in_(chunk))).scalars():
            scores[s.answer_id][s.metric] = s.value
    return [{
        "answer_id": a.id, "question_id": q.id, "family": q.family, "hop_count": q.hop_count,
        "premise": q.meta.get("premise") if q.family == "false_premise" else None,
        "latency_ms": a.latency_ms, "cost_usd": a.cost_usd, "tokens": a.tokens, "error": a.error,
        "scores": scores.get(a.id, {}),
    } for a, q in answers]


def _avg(values: list[float]) -> float | None:
    return round(mean(values), 4) if values else None


def summarize(rows: list[dict]) -> dict:
    qa = [r for r in rows if r["family"] != "codegen"]
    code = [r for r in rows if r["family"] == "codegen"]
    f1 = [r["scores"].get("f1", 0.0) for r in qa]
    by_family: dict[str, list[float]] = defaultdict(list)
    for r in qa:
        by_family[r["family"]].append(r["scores"].get("f1", 0.0))
    # E2 is defined on impact questions, which are balanced over k = 1, 2, 3 hops. Inheritance questions record
    # the depth of their deepest subclass instead, so they are kept on a separate curve.
    by_hop: dict[int, list[float]] = defaultdict(list)
    by_depth: dict[int, list[float]] = defaultdict(list)
    for r in qa:
        if r["family"] == "impact" and r["hop_count"]:
            by_hop[r["hop_count"]].append(r["scores"].get("f1", 0.0))
        elif r["family"] == "inheritance" and r["hop_count"]:
            by_depth[r["hop_count"]].append(r["scores"].get("f1", 0.0))
    ci = bootstrap_ci(f1)
    errors = sum(1 for r in rows if r["error"])
    summary = {
        # `f1` counts a failed call as 0, so every row is over the same questions; `f1_answered` leaves failed calls
        # out, so a run hit by rate limits shows the model's quality next to its error count.
        "n": len(rows), "errors": errors, "error_rate": round(errors / len(rows), 4) if rows else None,
        "f1": ci["mean"], "f1_ci": [ci["lo"], ci["hi"]],
        "f1_answered": _avg([r["scores"].get("f1", 0.0) for r in qa if not r["error"]]),
        "f1_by_family": {k: _avg(v) for k, v in sorted(by_family.items())},
        "n_by_family": {k: len(v) for k, v in sorted(by_family.items())},
        "f1_by_hop": {str(k): _avg(v) for k, v in sorted(by_hop.items())},
        "f1_by_inheritance_depth": {str(k): _avg(v) for k, v in sorted(by_depth.items())},
        "cost_per_question": _avg([r["cost_usd"] for r in rows]),
        "latency_ms_mean": _avg([r["latency_ms"] for r in rows]),
        "latency_ms_median": round(median([r["latency_ms"] for r in rows]), 1) if rows else None,
        "tokens_per_question": _avg([r["tokens"] for r in rows]),
    }
    for metric in EXTRA_METRICS:
        values = [r["scores"][metric] for r in qa if metric in r["scores"]]
        summary[metric] = _avg(values)
    false_premises = [r for r in qa if r["family"] == "false_premise" and r["premise"] is False]
    summary["false_premise_rejection_rate"] = _avg([r["scores"].get("premise_rejected", 0.0) for r in false_premises])
    if code:
        summary["codegen"] = {m: _avg([r["scores"][m] for r in code if m in r["scores"]]) for m in CODEGEN_METRICS}
        summary["codegen"]["n"] = len(code)
    return summary


def leaderboard(db: Session, dataset_id: int) -> list[dict]:
    exps = db.execute(select(Experiment).where(Experiment.dataset_id == dataset_id).order_by(Experiment.id)).scalars()
    board = []
    for e in exps:
        summary = e.summary or summarize(answer_rows(db, e.id))
        board.append({"experiment_id": e.id, "model": e.model, "pipeline": e.pipeline, "status": e.status,
                      "prompt_version": e.prompt_version, **summary})
    return sorted(board, key=lambda r: (r.get("f1") is None, -(r.get("f1") or 0)))


def hop_curve(db: Session, dataset_id: int) -> list[dict]:
    points = []
    for row in leaderboard(db, dataset_id):
        for hop, value in (row.get("f1_by_hop") or {}).items():
            points.append({"model": row["model"], "pipeline": row["pipeline"], "hop": int(hop), "f1": value})
    return points


def compare(db: Session, experiment_a: int, experiment_b: int, metric: str = "f1") -> dict:
    """Paired difference on the questions both experiments answered; a failed call on either side drops the pair."""
    rows_a, rows_b = answer_rows(db, experiment_a), answer_rows(db, experiment_b)
    failed = {r["question_id"] for r in rows_a + rows_b if r["error"]}

    def per_question(rows: list[dict]) -> dict[int, float]:
        return {r["question_id"]: r["scores"].get(metric, 0.0) for r in rows if r["question_id"] not in failed}
    return {"metric": metric, "a": experiment_a, "b": experiment_b, "excluded_failed_calls": len(failed),
            **paired_bootstrap(per_question(rows_a), per_question(rows_b))}
