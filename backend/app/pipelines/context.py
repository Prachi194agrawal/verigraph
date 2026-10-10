"""Formatting helpers for retrieved context."""

from __future__ import annotations

MAX_CODE_LINES = 60
MAX_CODE_CHARS = 2500


def code_chunk(qualified_name: str, file_path: str | None, start: int | None, end: int | None, code: str | None) -> str:
    lines = (code or "").splitlines()
    body = "\n".join(lines[:MAX_CODE_LINES])[:MAX_CODE_CHARS]
    if len(lines) > MAX_CODE_LINES or len(code or "") > MAX_CODE_CHARS:
        body += "\n    ..."
    return f"### {qualified_name} ({file_path}:{start}-{end})\n```python\n{body}\n```"


def clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "\n[context truncated]"
