"""Baseline: the question alone. Whatever the model gets right here comes from memory (experiment E3)."""

from __future__ import annotations

from app.pipelines.base import Pipeline


class NoRetrievalPipeline(Pipeline):
    name = "none"
