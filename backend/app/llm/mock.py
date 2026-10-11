"""Offline test doubles, selected with model names starting with "mock/".

- mock/empty       always answers with an empty list (a model that never commits to anything).
- mock/extractive  copies the sources of `a -REL-> b` lines in the retrieved context whose target is a
                   symbol named in the question. It has no knowledge of its own, so its score measures
                   what the retrieval put in front of it: zero without retrieval, high when the graph
                   context contains the answer. CI uses it to catch retrieval regressions for free.

Both also answer the internal prompts (summaries, feature tags, rephrasing, claim extraction, judges) with
well-formed placeholder JSON, so those code paths run end to end in tests without an API key.
"""

from __future__ import annotations

import json
import re

_BACKTICK = re.compile(r"`([^`]+)`")
_EDGE = re.compile(r"^\s*(\S+)\s+-([A-Z_]+)->\s+(\S+)")
_FUNCTION_IDS = re.compile(r"^(?:### )?(f\d+):", re.MULTILINE)


def _matches(name: str, target: str) -> bool:
    name, target = name.strip("`()"), target.strip("`()")
    return name == target or name.endswith("." + target) or target.endswith("." + name)


def _internal_prompt(user: str) -> str | None:
    """Placeholder JSON for the pipeline-internal prompts in app/llm/prompts/."""
    if user.startswith("Summarize each Python function"):
        return json.dumps({fid: "Mock summary of what this function does." for fid in _FUNCTION_IDS.findall(user)})
    if "List 6 to 12 features" in user:
        return json.dumps({"features": [{"name": "mock feature", "description": "A placeholder feature."}]})
    if user.startswith("Features of the repository"):
        ids = _FUNCTION_IDS.findall(user)
        return json.dumps({fid: [{"feature": "mock feature", "confidence": 0.99}] for fid in ids[:1]})
    if user.startswith("Rewrite this question"):
        question = user.rsplit("Question:", 1)[-1].strip()
        return json.dumps({"question": f"Quick question: {question}"})
    if user.startswith("Extract the factual claims"):
        return json.dumps({"claims": []})
    if user.startswith("You check whether an answer's explanation"):
        return json.dumps({"statements": [{"text": "placeholder", "supported": True}]})
    if user.startswith("Here is a Python function from the repository"):
        return json.dumps({"label": "faithful", "reason": "placeholder"})
    return None


def mock_complete(model: str, messages: list[dict]) -> str:
    kind = model.split("/", 1)[1] if "/" in model else model
    system = next((m["content"] for m in messages if m["role"] == "system"), "")
    user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
    internal = _internal_prompt(user)
    if internal is not None:
        return internal
    question = user.rsplit("Question:", 1)[-1]
    if "```python" in system or "python code block" in system:  # code-writing task
        sig = _BACKTICK.search(question)
        name = (sig.group(1) if sig else "generated").split("(")[0].split(".")[-1]
        return f"```python\ndef {name}(*args, **kwargs):\n    raise NotImplementedError\n```"
    if kind == "empty":
        return json.dumps({"answer": [], "explanation": "I do not know."})
    if kind != "extractive":
        raise ValueError(f"unknown mock model {model!r}; use mock/empty or mock/extractive")
    targets = _BACKTICK.findall(question)
    answer: list[str] = []
    claims: list[str] = []
    context = user.rsplit("Question:", 1)[0]
    for line in context.splitlines():
        m = _EDGE.match(line)
        if not m:
            continue
        src, rel, dst = m.groups()
        if rel == "LANGUAGE" and any(_matches(dst, t) for t in targets) and src not in answer:
            answer.append(src)
            continue
        if rel in ("CALLS", "INHERITS") and any(_matches(dst, t) for t in targets) and src not in answer:
            answer.append(src)
            verb = "calls" if rel == "CALLS" else "inherits from"
            claims.append(f"`{src}` {verb} `{dst}`.")
    return json.dumps({"answer": answer[:10], "explanation": " ".join(claims[:10]) or "No evidence in context."})
