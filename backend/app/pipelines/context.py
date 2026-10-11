"""Formatting helpers for retrieved context."""

from __future__ import annotations

from app.graph.languages import detect_language

MAX_CODE_LINES = 60
MAX_CODE_CHARS = 2500


def code_chunk(qualified_name: str, file_path: str | None, start: int | None, end: int | None, code: str | None,
               language: str | None = None) -> str:
    lines = (code or "").splitlines()
    body = "\n".join(lines[:MAX_CODE_LINES])[:MAX_CODE_CHARS]
    if len(lines) > MAX_CODE_LINES or len(code or "") > MAX_CODE_CHARS:
        body += "\n    ..."
    spec = detect_language(file_path or "")
    language = language or (spec.name if spec else "text")
    if language in ("other", "unknown"):
        language = "text"
    return f"### {qualified_name} ({file_path}:{start}-{end})\n```{language}\n{body}\n```"


def clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "\n[context truncated]"
