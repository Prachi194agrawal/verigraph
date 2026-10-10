"""Set precision, recall and F1 for list answers, plus hit@k for ranked localization answers."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SetScore:
    precision: float
    recall: float
    f1: float
    true_positives: list[str] = field(default_factory=list)
    false_positives: list[str] = field(default_factory=list)
    false_negatives: list[str] = field(default_factory=list)


def set_f1(predicted: list[str], gold: list[str]) -> SetScore:
    """Empty prediction for an empty gold set (a correctly rejected false premise) scores 1.0."""
    pred, ref = set(predicted), set(gold)
    tp, fp, fn = pred & ref, pred - ref, ref - pred
    if not pred and not ref:
        return SetScore(1.0, 1.0, 1.0)
    precision = len(tp) / len(pred) if pred else 0.0
    recall = len(tp) / len(ref) if ref else (1.0 if not pred else 0.0)
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return SetScore(precision, recall, f1, sorted(tp), sorted(fp), sorted(fn))


def hit_at_k(ranked: list[str], gold: list[str], k: int) -> float:
    return 1.0 if set(ranked[:k]) & set(gold) else 0.0
