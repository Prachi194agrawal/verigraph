"""LLM-judge metrics, used only where the graph has no exact answer.

faithfulness(): Ragas-style faithfulness of an explanation to the retrieved context: the judge splits the
explanation into statements and marks each as supported by the context or not; the score is the
supported fraction. Use a judge from a different model family than the model under test.
"""

from __future__ import annotations

from app.llm.client import LLMClient
from app.llm.prompts import render

MAX_CONTEXT_CHARS = 12_000


def faithfulness(llm: LLMClient, judge_model: str, context: str, explanation: str) -> float | None:
    if not context.strip() or not (explanation or "").strip():
        return None
    data, _ = llm.complete_json(judge_model, [{"role": "user", "content": render(
        "faithfulness", context=context[:MAX_CONTEXT_CHARS], explanation=explanation)}],
        max_tokens=900, name="faithfulness")
    statements = data.get("statements") if isinstance(data, dict) else None
    if not statements:
        return None
    flags = [bool(s.get("supported")) for s in statements if isinstance(s, dict)]
    return sum(flags) / len(flags) if flags else None


def judge_semantic(llm: LLMClient, judge_model: str, repo: str, code: str, claim_kind: str, claim: str) -> str | None:
    """'faithful' / 'unfaithful' for an LLM-written summary or feature tag (LLM-layer audit)."""
    data, _ = llm.complete_json(judge_model, [{"role": "user", "content": render(
        "judge_semantic", repo=repo, code=code[:6000], claim_kind=claim_kind, claim=claim)}],
        max_tokens=200, name="judge_semantic")
    label = data.get("label") if isinstance(data, dict) else None
    return label if label in ("faithful", "unfaithful") else None
