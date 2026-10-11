"""Vector RAG: embed the question, take the k nearest function chunks from Neo4j's vector index."""

from __future__ import annotations

from app.pipelines.base import Pipeline, QuestionInput, Retrieval
from app.pipelines.context import clip, code_chunk
from app.semantic.embeddings import get_embedder

MAX_CONTEXT_CHARS = 16_000


class VectorPipeline(Pipeline):
    name = "vector"

    def retrieve(self, q: QuestionInput) -> Retrieval:
        if not self.ctx.index.functions:
            return Retrieval("This build contains file inventory only. No indexed function snippets "
                             "are available for vector retrieval.")
        k = int(self.ctx.options.get("top_k", 8))
        embedder = get_embedder(self.ctx.embedding_model)
        vector = embedder.embed_queries([q.text])[0].tolist()
        blank = q.meta.get("blank")  # code-writing tasks: never show the function being rewritten
        rows = [r for r in self.ctx.store.vector_search(self.ctx.build_id, vector, k + 1)
                if r["qualified_name"] != blank][:k]
        context = "\n\n".join(code_chunk(r["qualified_name"], r["file_path"], r["start_line"], r["end_line"], r["code"],
                                       r.get("language"))
                              for r in rows)
        context = clip(context, MAX_CONTEXT_CHARS)
        nodes = [n for n in dict.fromkeys([r["qualified_name"] for r in rows] + [r["file_path"] for r in rows])
                 if n in context]  # chunks cut by truncation were not shown to the model
        return Retrieval(context, nodes)
