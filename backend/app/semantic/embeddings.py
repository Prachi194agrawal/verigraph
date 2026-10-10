"""Code embeddings, stored on Function nodes and searched through Neo4j's vector index.

EMBEDDING_MODEL selects the embedder:
- a sentence-transformers model id, e.g. "nomic-ai/CodeRankEmbed" (default, 768-d, 137M params) or
  "jinaai/jina-code-embeddings-0.5b" (needs `pip install -e '.[embeddings]'`);
- "hash": a dependency-free lexical embedder (signed feature hashing of identifier sub-tokens). It is not
  semantic, but it is deterministic and instant, so tests and the CI gate use it.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from functools import lru_cache
from typing import Protocol

import numpy as np

from app.config import get_settings
from app.graph.store import SUMMARY_VECTOR_INDEX, VECTOR_INDEX, GraphStore

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d+")
_SUBTOKEN = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")
MAX_EMBED_CHARS = 4000


class Embedder(Protocol):
    name: str
    dim: int

    def embed_documents(self, texts: list[str]) -> np.ndarray: ...

    def embed_queries(self, texts: list[str]) -> np.ndarray: ...


def code_tokens(text: str) -> list[str]:
    """Identifiers plus their snake_case/camelCase parts: `parse_headers` -> parse_headers, parse, headers."""
    out: list[str] = []
    for ident in _IDENT.findall(text):
        out.append(ident.lower())
        parts = [p.lower() for chunk in ident.split("_") for p in _SUBTOKEN.findall(chunk)]
        if len(parts) > 1:
            out.extend(parts)
    return out


class HashEmbedder:
    def __init__(self, dim: int = 768) -> None:
        self.name, self.dim = "hash", dim

    def _vector(self, text: str) -> np.ndarray:
        v = np.zeros(self.dim, dtype=np.float32)
        tokens = code_tokens(text)
        for feature in tokens + [f"{a} {b}" for a, b in zip(tokens, tokens[1:], strict=False)]:
            h = int.from_bytes(hashlib.blake2b(feature.encode(), digest_size=8).digest(), "little")
            v[h % self.dim] += 1.0 if (h >> 63) & 1 else -1.0
        v = np.sign(v) * np.log1p(np.abs(v))
        norm = float(np.linalg.norm(v))
        return v / norm if norm else v

    def embed_documents(self, texts: list[str]) -> np.ndarray:
        return np.stack([self._vector(t) for t in texts]) if texts else np.zeros((0, self.dim), np.float32)

    embed_queries = embed_documents


class SentenceTransformerEmbedder:
    # Retrieval models expect instruction prefixes on queries (and some on documents).
    QUERY_PREFIX = {
        "nomic-ai/CodeRankEmbed": "Represent this query for searching relevant code: ",
        "jinaai/jina-code-embeddings-0.5b": "Find the most relevant code snippet given the following query:\n",
        "jinaai/jina-code-embeddings-1.5b": "Find the most relevant code snippet given the following query:\n",
    }
    DOC_PREFIX = {
        "jinaai/jina-code-embeddings-0.5b": "Candidate code snippet:\n",
        "jinaai/jina-code-embeddings-1.5b": "Candidate code snippet:\n",
    }

    def __init__(self, model_name: str, batch_size: int = 16, max_tokens: int = 512) -> None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise RuntimeError(
                f"EMBEDDING_MODEL={model_name} needs the embeddings extra (pip install -e '.[embeddings]'); "
                "or set EMBEDDING_MODEL=hash") from exc
        self.name = model_name
        self.batch_size = batch_size
        self.model = SentenceTransformer(model_name, trust_remote_code=True)
        self.model.max_seq_length = min(self.model.max_seq_length or max_tokens, max_tokens)
        get_dim = getattr(self.model, "get_embedding_dimension", None) or self.model.get_sentence_embedding_dimension
        self.dim = int(get_dim())

    def _encode(self, texts: list[str], prefix: str) -> np.ndarray:
        return np.asarray(self.model.encode([prefix + t for t in texts], batch_size=self.batch_size,
                                            normalize_embeddings=True, show_progress_bar=False), dtype=np.float32)

    def embed_documents(self, texts: list[str]) -> np.ndarray:
        return self._encode(texts, self.DOC_PREFIX.get(self.name, ""))

    def embed_queries(self, texts: list[str]) -> np.ndarray:
        return self._encode(texts, self.QUERY_PREFIX.get(self.name, ""))


@lru_cache
def get_embedder(name: str | None = None) -> Embedder:
    s = get_settings()
    name = name or s.embedding_model
    if name == "hash":
        return HashEmbedder(s.embedding_dim)
    return SentenceTransformerEmbedder(name, s.embedding_batch_size, s.embedding_max_tokens)


def function_text(row: dict) -> str:
    parts = [row["qualified_name"], row.get("signature") or "", row.get("docstring") or "", row.get("code") or ""]
    return "\n".join(p for p in parts if p)[:MAX_EMBED_CHARS]


def embed_functions(store: GraphStore, build_id: int, embedder: Embedder,
                    progress: Callable[[str], None] | None = None, batch: int = 64) -> dict:
    """Write `code_embedding` (and `summary_embedding` where a summary exists) on every Function node."""
    rows = store.read(
        "MATCH (f:Function {build_id: $b}) RETURN f.qualified_name AS qualified_name, f.signature AS signature, "
        "f.docstring AS docstring, f.code AS code, f.summary AS summary ORDER BY f.qualified_name", {"b": build_id})
    store.ensure_vector_index(VECTOR_INDEX, "code_embedding", embedder.dim)
    for i in range(0, len(rows), batch):
        if progress:
            progress(f"embedding functions {i}/{len(rows)}")
        chunk = rows[i:i + batch]
        vectors = embedder.embed_documents([function_text(r) for r in chunk])
        store.set_function_props(build_id, [
            {"qualified_name": r["qualified_name"], "props": {"code_embedding": v.tolist(), "embedding_model": embedder.name}}
            for r, v in zip(chunk, vectors, strict=True)])
    summarized = [r for r in rows if r.get("summary")]
    if summarized:
        store.ensure_vector_index(SUMMARY_VECTOR_INDEX, "summary_embedding", embedder.dim)
        for i in range(0, len(summarized), batch):
            chunk = summarized[i:i + batch]
            vectors = embedder.embed_documents([r["summary"] for r in chunk])
            store.set_function_props(build_id, [
                {"qualified_name": r["qualified_name"], "props": {"summary_embedding": v.tolist()}}
                for r, v in zip(chunk, vectors, strict=True)])
    return {"embedded_functions": len(rows), "embedded_summaries": len(summarized),
            "embedding_model": embedder.name, "embedding_dim": embedder.dim}
