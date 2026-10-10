"""Tracing: one trace per answered question, holding the prompt, the retrieved subgraph and the scores.

JSONL files under <data_dir>/traces are always written. When LANGFUSE_PUBLIC_KEY/SECRET_KEY are set,
LLM generations reach Langfuse through LiteLLM's "langfuse" callback (same trace_id) and retrieval
spans and scores are sent with the Langfuse SDK (v2 API).
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from functools import lru_cache
from pathlib import Path

from app.config import get_settings

log = logging.getLogger(__name__)


class Tracer:
    def __init__(self, directory: Path, langfuse_keys: tuple[str, str, str | None] | None = None) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / f"traces-{time.strftime('%Y%m%d')}.jsonl"
        self._lock = threading.Lock()
        self._langfuse = None
        if langfuse_keys:
            try:
                from langfuse import Langfuse

                public, secret, host = langfuse_keys
                self._langfuse = Langfuse(public_key=public, secret_key=secret, host=host)
            except Exception as exc:  # noqa: BLE001 - tracing must never break a run
                log.warning("Langfuse disabled: %s", exc)

    @staticmethod
    def new_trace_id() -> str:
        return uuid.uuid4().hex

    def log(self, trace_id: str, kind: str, payload: dict) -> None:
        line = json.dumps({"ts": time.time(), "trace_id": trace_id, "kind": kind, **payload}, default=str)
        with self._lock, self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        if self._langfuse is not None and kind != "generation":  # generations arrive via LiteLLM
            try:
                self._langfuse.span(trace_id=trace_id, name=kind, input=payload.get("input"),
                                    output=payload.get("output"), metadata=payload.get("metadata"))
            except Exception as exc:  # noqa: BLE001
                log.debug("langfuse span failed: %s", exc)

    def score(self, trace_id: str, name: str, value: float, comment: str | None = None) -> None:
        self.log(trace_id, "score", {"name": name, "value": value, "comment": comment})
        if self._langfuse is not None:
            try:
                self._langfuse.score(trace_id=trace_id, name=name, value=float(value), comment=comment)
            except Exception as exc:  # noqa: BLE001
                log.debug("langfuse score failed: %s", exc)

    def flush(self) -> None:
        if self._langfuse is not None:
            try:
                self._langfuse.flush()
            except Exception as exc:  # noqa: BLE001
                log.debug("langfuse flush failed: %s", exc)


@lru_cache
def get_tracer() -> Tracer:
    s = get_settings()
    keys = (s.langfuse_public_key, s.langfuse_secret_key, s.langfuse_host) if s.langfuse_enabled else None
    return Tracer(s.data_dir / "traces", keys)
