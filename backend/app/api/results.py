"""Leaderboard, hop curve, paired comparisons, per-answer checks, human labels and agreement (kappa)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.eval.meta_eval import cohens_kappa
from app.eval.report import compare, hop_curve, leaderboard
from app.models import (
    Answer,
    ClaimVerdict,
    Dataset,
    Experiment,
    HumanLabel,
    Question,
    Score,
    SymbolCheck,
    TestRun,
)
from app.schemas import LabelCreate

router = APIRouter(tags=["results"])


@router.get("/leaderboard")
def get_leaderboard(dataset_id: int, db: Session = Depends(get_db)) -> list[dict]:
    return leaderboard(db, dataset_id)


@router.get("/leaderboard/hops")
def get_hop_curve(dataset_id: int, db: Session = Depends(get_db)) -> list[dict]:
    return hop_curve(db, dataset_id)


@router.get("/compare")
def compare_experiments(a: int, b: int, metric: str = "f1", db: Session = Depends(get_db)) -> dict:
    return compare(db, a, b, metric)


@router.get("/answers/{answer_id}/checks")
def answer_checks(answer_id: int, db: Session = Depends(get_db)) -> dict:
    answer = db.get(Answer, answer_id)
    if answer is None:
        raise HTTPException(404, "answer not found")
    question = db.get(Question, answer.question_id)
    exp = db.get(Experiment, answer.experiment_id)
    claims = db.execute(select(ClaimVerdict).where(ClaimVerdict.answer_id == answer_id)).scalars()
    symbols = db.execute(select(SymbolCheck).where(SymbolCheck.answer_id == answer_id)).scalars()
    runs = db.execute(select(TestRun).where(TestRun.answer_id == answer_id)).scalars()
    scores = db.execute(select(Score).where(Score.answer_id == answer_id)).scalars()
    labels = db.execute(select(HumanLabel).where(HumanLabel.answer_id == answer_id)).scalars()
    dataset = db.get(Dataset, exp.dataset_id)
    return {
        "build_id": dataset.build_id if dataset else None,  # for links into the graph explorer
        "answer": {"id": answer.id, "text": answer.text, "parsed_items": answer.parsed_items, "match": answer.match,
                   "explanation": answer.explanation, "code": answer.code, "retrieved_nodes": answer.retrieved_nodes,
                   "steps": answer.steps, "latency_ms": answer.latency_ms, "tokens": answer.tokens,
                   "cost_usd": answer.cost_usd, "error": answer.error, "trace_id": answer.trace_id},
        "question": {"id": question.id, "family": question.family, "text": question.text,
                     "gold_answer": question.gold_answer, "gold_cypher": question.gold_cypher,
                     "hop_count": question.hop_count, "meta": question.meta, "build_params": question.gold_params},
        "experiment": {"id": exp.id, "model": exp.model, "pipeline": exp.pipeline, "dataset_id": exp.dataset_id},
        "claims": [{"id": c.id, "subject": c.subject, "relation": c.relation, "object": c.object,
                    "verdict": c.verdict, "evidence_edge": c.evidence_edge, "extractor": c.extractor,
                    "claim_text": c.claim_text} for c in claims],
        "symbols": [{"symbol": s.symbol, "kind": s.kind, "exists": s.exists, "arity_ok": s.arity_ok,
                     "category": s.category, "detail": s.detail, "line": s.line} for s in symbols],
        "test_runs": [{"tests_run": t.tests_run, "passed": t.passed, "failed": t.failed, "status": t.status,
                       "log_path": t.log_path} for t in runs],
        "scores": {s.metric: s.value for s in scores},
        "labels": [{"id": h.id, "label": h.label, "claim_verdict_id": h.claim_verdict_id, "notes": h.notes}
                   for h in labels],
    }


@router.post("/answers/{answer_id}/labels", status_code=201)
def add_label(answer_id: int, body: LabelCreate, db: Session = Depends(get_db)) -> dict:
    if db.get(Answer, answer_id) is None:
        raise HTTPException(404, "answer not found")
    if body.claim_verdict_id is not None:
        claim = db.get(ClaimVerdict, body.claim_verdict_id)
        if claim is None or claim.answer_id != answer_id:
            raise HTTPException(422, "claim verdict does not belong to this answer")
    label = HumanLabel(answer_id=answer_id, claim_verdict_id=body.claim_verdict_id, label=body.label, notes=body.notes)
    db.add(label)
    db.flush()
    return {"id": label.id}


@router.get("/datasets/{dataset_id}/agreement")
def agreement(dataset_id: int, db: Session = Depends(get_db)) -> dict:
    """Cohen's kappa between each automatic checker and the human labels (experiment E6)."""
    rows = db.execute(
        select(HumanLabel, Answer, Question).join(Answer, HumanLabel.answer_id == Answer.id)
        .join(Question, Answer.question_id == Question.id).where(Question.dataset_id == dataset_id)).all()
    claim_pairs, answer_pairs, code_pairs = [], [], []
    for label, answer, question in rows:
        if label.claim_verdict_id is not None:
            verdict = db.get(ClaimVerdict, label.claim_verdict_id)
            if verdict is not None:
                claim_pairs.append((verdict.verdict, label.label))
            continue
        scores = {s.metric: s.value for s in db.execute(select(Score).where(Score.answer_id == answer.id)).scalars()}
        if question.family == "codegen":
            code_pairs.append(("hallucinated" if scores.get("hallucinated") else "clean", label.label))
        else:
            answer_pairs.append(("correct" if scores.get("exact_match") else "incorrect", label.label))

    def summary(pairs: list[tuple[str, str]]) -> dict:
        return {"n": len(pairs), "kappa": cohens_kappa([a for a, _ in pairs], [b for _, b in pairs]),
                "raw_agreement": sum(a == b for a, b in pairs) / len(pairs) if pairs else None}

    return {"claim_checker": summary(claim_pairs), "set_f1_exact_match": summary(answer_pairs),
            "symbol_checker": summary(code_pairs)}
