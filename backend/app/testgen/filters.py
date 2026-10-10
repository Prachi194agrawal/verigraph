"""Filters applied to every draft question before it enters a dataset."""

from __future__ import annotations

from app.testgen.templates import Draft

MIN_GOLD, MAX_GOLD = 1, 10


def gold_size_ok(d: Draft) -> bool:
    """Keep gold sets of 1-10 items so list answers can be scored without a judge. False-premise questions
    are the exception: their gold is deliberately empty."""
    if d.family == "false_premise":
        return len(d.gold_answer) in (0, 2)
    if d.family == "codegen":
        return len(d.gold_answer) <= MAX_GOLD
    return MIN_GOLD <= len(d.gold_answer) <= MAX_GOLD


def keep(d: Draft, seen: set[tuple[str, str]]) -> bool:
    key = (d.family, d.text)
    if key in seen or not gold_size_ok(d):
        return False
    seen.add(key)
    return True
