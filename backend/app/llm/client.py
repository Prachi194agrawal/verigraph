"""LLM gateway. Every model call in the project goes through `LLMClient.complete`.

LiteLLM gives one API for Groq ("groq/..."), Gemini ("gemini/..."), OpenAI ("openai/..." or bare names),
Anthropic ("anthropic/...") and Ollama ("ollama/..."); it also reports token usage and cost. Responses at
temperature 0 are cached on disk (keyed by model + messages + parameters), including their original
latency and cost, so a cached rerun reports the same numbers as the first run.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass
from functools import lru_cache

from app.config import Settings, get_settings
from app.llm.cache import ResponseCache, cache_key
from app.llm.mock import mock_complete
from app.llm.tracing import Tracer, get_tracer

log = logging.getLogger(__name__)


@dataclass
class LLMResponse:
    text: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    cached: bool = False


class LLMClient:
    def __init__(self, settings: Settings | None = None, tracer: Tracer | None = None) -> None:
        self.settings = settings or get_settings()
        self.tracer = tracer or get_tracer()
        self.cache = ResponseCache(self.settings.data_dir / "llm_cache.sqlite") if self.settings.llm_cache else None
        self._litellm = None

    def _lib(self):
        if self._litellm is None:
            import litellm

            litellm.drop_params = True  # e.g. response_format for providers that lack it
            litellm.suppress_debug_info = True
            if self.settings.langfuse_enabled:
                os.environ.setdefault("LANGFUSE_PUBLIC_KEY", self.settings.langfuse_public_key or "")
                os.environ.setdefault("LANGFUSE_SECRET_KEY", self.settings.langfuse_secret_key or "")
                if self.settings.langfuse_host:
                    os.environ.setdefault("LANGFUSE_HOST", self.settings.langfuse_host)
                litellm.success_callback = ["langfuse"]
                litellm.failure_callback = ["langfuse"]
            self._litellm = litellm
        return self._litellm

    def complete(self, model: str, messages: list[dict], *, temperature: float = 0.0, max_tokens: int = 1024,
                 json_mode: bool = False, trace_id: str | None = None, name: str = "completion") -> LLMResponse:
        payload = {"model": model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens,
                   "json_mode": json_mode}
        key = cache_key(payload)
        if self.cache is not None and temperature == 0:
            hit = self.cache.get(key)
            if hit is not None:
                resp = LLMResponse(**{**hit, "cached": True})
                self._trace(trace_id, name, messages, resp)
                return resp

        if model.startswith("mock/"):
            started = time.perf_counter()
            text = mock_complete(model, messages)
            resp = LLMResponse(text=text, model=model, latency_ms=(time.perf_counter() - started) * 1000)
        else:
            resp = self._call_litellm(model, messages, temperature, max_tokens, json_mode, trace_id, name)

        if self.cache is not None and temperature == 0:
            self.cache.put(key, {k: v for k, v in asdict(resp).items() if k != "cached"})
        self._trace(trace_id, name, messages, resp)
        return resp

    def _call_litellm(self, model: str, messages: list[dict], temperature: float, max_tokens: int,
                      json_mode: bool, trace_id: str | None, name: str) -> LLMResponse:
        litellm = self._lib()
        kwargs: dict = {"model": model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens,
                        "timeout": self.settings.llm_timeout_s, "num_retries": self.settings.llm_num_retries}
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        if model.startswith(("ollama/", "ollama_chat/")) and self.settings.ollama_api_base:
            kwargs["api_base"] = self.settings.ollama_api_base
        if trace_id:
            kwargs["metadata"] = {"trace_id": trace_id, "generation_name": name}
        started = time.perf_counter()
        r = litellm.completion(**kwargs)
        latency = (time.perf_counter() - started) * 1000
        usage = getattr(r, "usage", None)
        try:
            cost = float(litellm.completion_cost(completion_response=r) or 0.0)
        except Exception:  # noqa: BLE001 - unknown prices (e.g. local models) cost nothing
            cost = 0.0
        return LLMResponse(
            text=r.choices[0].message.content or "", model=model,
            prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(usage, "completion_tokens", 0) or 0, cost_usd=cost, latency_ms=latency,
        )

    def _trace(self, trace_id: str | None, name: str, messages: list[dict], resp: LLMResponse) -> None:
        if trace_id:
            self.tracer.log(trace_id, "generation", {
                "name": name, "model": resp.model, "input": messages, "output": resp.text, "cached": resp.cached,
                "usage": {"prompt": resp.prompt_tokens, "completion": resp.completion_tokens},
                "cost_usd": resp.cost_usd, "latency_ms": round(resp.latency_ms, 1),
            })

    def complete_json(self, model: str, messages: list[dict], **kwargs) -> tuple[dict | list | None, LLMResponse]:
        resp = self.complete(model, messages, json_mode=True, **kwargs)
        return extract_json(resp.text), resp


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json(text: str) -> dict | list | None:
    """Best-effort JSON from a model reply: whole text, fenced block, or the first balanced {...}/[...]."""
    text = (text or "").strip()
    candidates = [text, *(m.group(1).strip() for m in _FENCE.finditer(text))]
    for c in candidates:
        try:
            return json.loads(c)
        except (json.JSONDecodeError, ValueError):
            pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        while start != -1:
            depth, in_str, esc = 0, False, False
            for i in range(start, len(text)):
                ch = text[i]
                if in_str:
                    if esc:
                        esc = False
                    elif ch == "\\":
                        esc = True
                    elif ch == '"':
                        in_str = False
                    continue
                if ch == '"':
                    in_str = True
                elif ch == opener:
                    depth += 1
                elif ch == closer:
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(text[start:i + 1])
                        except (json.JSONDecodeError, ValueError):
                            break
            start = text.find(opener, start + 1)
    return None


@lru_cache
def get_llm() -> LLMClient:
    return LLMClient()
