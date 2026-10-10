"""Evaluating the evaluation: agreement with humans, confidence intervals, and graph edge audits."""

from __future__ import annotations

import random
from collections import Counter, defaultdict
from collections.abc import Sequence

import numpy as np

from app.graph.store import GraphStore


def cohens_kappa(a: Sequence[str], b: Sequence[str]) -> float | None:
    """Agreement between two labelers beyond chance. None when undefined (fewer than 2 items)."""
    if len(a) != len(b) or len(a) < 2:
        return None
    n = len(a)
    observed = sum(x == y for x, y in zip(a, b, strict=True)) / n
    ca, cb = Counter(a), Counter(b)
    expected = sum(ca[k] * cb[k] for k in set(ca) | set(cb)) / (n * n)
    if expected == 1.0:
        return 1.0 if observed == 1.0 else 0.0
    return (observed - expected) / (1 - expected)


def bootstrap_ci(values: Sequence[float], n_boot: int = 2000, alpha: float = 0.05, seed: int = 0) -> dict:
    """Mean with a percentile bootstrap 95% CI."""
    v = np.asarray(values, dtype=float)
    if v.size == 0:
        return {"mean": None, "lo": None, "hi": None, "n": 0}
    rng = np.random.default_rng(seed)
    means = v[rng.integers(0, v.size, size=(n_boot, v.size))].mean(axis=1)
    lo, hi = np.quantile(means, [alpha / 2, 1 - alpha / 2])
    return {"mean": float(v.mean()), "lo": float(lo), "hi": float(hi), "n": int(v.size)}


def paired_bootstrap(a: dict, b: dict, n_boot: int = 2000, alpha: float = 0.05, seed: int = 0) -> dict:
    """Difference mean(a - b) over shared question ids, with a bootstrap CI and the share of resamples where
    the sign flips (a rough two-sided p-value)."""
    keys = sorted(set(a) & set(b))
    if not keys:
        return {"diff": None, "lo": None, "hi": None, "n": 0, "p": None}
    d = np.array([a[k] - b[k] for k in keys], dtype=float)
    rng = np.random.default_rng(seed)
    means = d[rng.integers(0, d.size, size=(n_boot, d.size))].mean(axis=1)
    lo, hi = np.quantile(means, [alpha / 2, 1 - alpha / 2])
    flips = float(np.mean(means <= 0)) if d.mean() > 0 else float(np.mean(means >= 0))
    return {"diff": float(d.mean()), "lo": float(lo), "hi": float(hi), "n": int(d.size), "p": min(1.0, 2 * flips)}


def sample_edges(store: GraphStore, build_id: int, per_resolver: int = 100, seed: int = 0) -> list[dict]:
    """A reproducible sample of CALLS edges per resolver, for hand-checking resolver precision."""
    rows = store.read(
        "MATCH (a:Function {build_id: $b})-[r:CALLS]->(c:Function) RETURN a.qualified_name AS caller, "
        "c.qualified_name AS callee, r.resolved_by AS resolved_by, r.line AS line, a.file_path AS file_path "
        "ORDER BY caller, callee", {"b": build_id})
    by_resolver: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_resolver[r["resolved_by"]].append(r)
    rng = random.Random(seed)
    sample = []
    for resolver in sorted(by_resolver):
        edges = by_resolver[resolver]
        sample.extend(rng.sample(edges, min(per_resolver, len(edges))))
    return sample


def edge_precision(labeled: list[dict]) -> dict[str, dict]:
    """Per resolver: how many sampled edges were checked and the share judged correct."""
    out: dict[str, dict] = {}
    for resolver in sorted({r["resolved_by"] for r in labeled}):
        rows = [r for r in labeled if r["resolved_by"] == resolver and r.get("label") in ("correct", "incorrect")]
        correct = sum(r["label"] == "correct" for r in rows)
        out[resolver] = {"checked": len(rows), "correct": correct,
                         "precision": correct / len(rows) if rows else None}
    return out
