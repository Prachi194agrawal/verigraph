"""Step 5: the answering pipelines compared on the leaderboard."""

from __future__ import annotations

from app.pipelines.base import Pipeline, PipelineContext
from app.pipelines.cypher_agent import CypherAgentPipeline
from app.pipelines.graphrag import GraphRAGPipeline
from app.pipelines.no_retrieval import NoRetrievalPipeline
from app.pipelines.vector import VectorPipeline

PIPELINES: dict[str, type[Pipeline]] = {
    "none": NoRetrievalPipeline,
    "vector": VectorPipeline,
    "graphrag": GraphRAGPipeline,
    "cypher_agent": CypherAgentPipeline,
}


def make_pipeline(name: str, ctx: PipelineContext) -> Pipeline:
    if name not in PIPELINES:
        raise ValueError(f"unknown pipeline {name!r}; choose from {sorted(PIPELINES)}")
    return PIPELINES[name](ctx)
