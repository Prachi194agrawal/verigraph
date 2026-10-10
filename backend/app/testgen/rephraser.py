"""Optional LLM rephrasing of template questions. The LLM only changes wording: every backticked symbol
must survive verbatim, otherwise the template text is kept, so gold answers stay valid."""

from __future__ import annotations

import re

from app.llm.client import LLMClient
from app.llm.prompts import render

_BACKTICK = re.compile(r"`[^`]+`")
SKIP_FAMILIES = {"localization", "codegen"}  # issue texts and code specs are already natural


def rephrase(llm: LLMClient, model: str, text: str) -> str:
    data, _ = llm.complete_json(model, [{"role": "user", "content": render("rephrase", question=text)}],
                                max_tokens=400, name="rephrase")
    new = data.get("question") if isinstance(data, dict) else None
    if not isinstance(new, str) or not new.strip():
        return text
    if any(span not in new for span in _BACKTICK.findall(text)):
        return text
    return " ".join(new.split())
