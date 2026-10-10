"""Parse model replies: the JSON answer list and explanation, or the code block for code-writing tasks."""

from __future__ import annotations

import re

from app.llm.client import extract_json

_CODE = re.compile(r"```(?:python|py)?[ \t]*\n(.*?)```", re.DOTALL)


def extract_code(text: str) -> str:
    blocks = _CODE.findall(text or "")
    if blocks:
        return max(blocks, key=len).strip("\n")
    return (text or "").strip() if "def " in (text or "") else ""


def _flatten(answer: object) -> list[str]:
    if answer is None:
        return []
    if isinstance(answer, str):
        parts = re.split(r"[\n,;]+", answer) if ("," in answer or "\n" in answer) else [answer]
        return [p.strip() for p in parts if p.strip()]
    if isinstance(answer, (int, float)):
        return [str(answer)]
    if isinstance(answer, dict):
        for key in ("qualified_name", "name", "path", "function", "file", "author", "value"):
            if key in answer:
                return _flatten(answer[key])
        return [str(v) for v in answer.values() if isinstance(v, (str, int))][:1]
    if isinstance(answer, (list, tuple)):
        out: list[str] = []
        for a in answer:
            out.extend(_flatten(a))
        return out
    return [str(answer)]


def parse_output(text: str, family: str) -> tuple[list[str], str, str | None]:
    """(answer items, explanation, code)."""
    if family == "codegen":
        return [], "", extract_code(text)
    data = extract_json(text)
    if isinstance(data, dict):
        answer = data.get("answer", data.get("answers", data.get("items")))
        return _flatten(answer), str(data.get("explanation") or ""), None
    if isinstance(data, list):
        return _flatten(data), "", None
    return [], (text or "").strip(), None
