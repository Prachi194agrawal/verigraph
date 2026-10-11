"""GraphRAG: find the symbols the question names, expand a few hops around them in the graph, and give the
model the subgraph as explicit edges (with how each edge was resolved) plus the seed's code.

The expansion is the same for every question family, and `graphrag_hops` (default 2) bounds how far callers
are followed, which is what experiment E2 measures: deeper questions eventually need more hops than this.
"""

from __future__ import annotations

import re

from app.config import PRECISE_RESOLVERS
from app.eval.normalize import clean_item
from app.pipelines.base import Pipeline, QuestionInput, Retrieval
from app.pipelines.context import clip, code_chunk

MAX_CONTEXT_CHARS = 16_000
_BACKTICK = re.compile(r"`([^`\n]+)`")
_IDENT = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+\b|\b[A-Za-z]+_[A-Za-z0-9_]+\b|\b[a-z]+[A-Z]\w*\b")


class GraphRAGPipeline(Pipeline):
    name = "graphrag"

    def find_seeds(self, q: QuestionInput) -> list[str]:
        index = self.ctx.index
        found: list[str] = []
        for span in _BACKTICK.findall(q.text) + _IDENT.findall(q.text):
            name = clean_item(span)
            if index.is_entity(name):
                found.append(name)
                continue
            matches = sorted(index.symbols_ending_with(name))
            if 0 < len(matches) <= 3:
                found.extend(matches)
        return list(dict.fromkeys(found))

    def vector_seeds(self, q: QuestionInput, k: int = 3) -> list[str]:
        if not self.ctx.embedding_model:
            return []
        from app.semantic.embeddings import get_embedder

        vector = get_embedder(self.ctx.embedding_model).embed_queries([q.text])[0].tolist()
        try:
            rows = self.ctx.store.vector_search(self.ctx.build_id, vector, k + 1)
        except Exception:  # noqa: BLE001 - no vector index for this build
            return []
        return [r["qualified_name"] for r in rows if r["qualified_name"] != q.meta.get("blank")][:k]

    def retrieve(self, q: QuestionInput) -> Retrieval:
        if q.family == "files" or not self.ctx.index.functions:
            language = q.meta.get("language") if q.family == "files" else None
            limit = int(q.meta.get("limit", 10)) if q.family == "files" else 30
            files = [path for path in sorted(self.ctx.index.files)
                     if language is None or self.ctx.index.file_languages[path] == language][:limit]
            edges = [f"{path} -LANGUAGE-> {self.ctx.index.file_languages[path]} "
                     f"[analysis: {self.ctx.index.analysis_levels[path]}]" for path in files]
            context = "Observed file inventory (alphabetically ordered paths):\n" + "\n".join(edges)
            return Retrieval(context, files)
        hops = int(self.ctx.options.get("graphrag_hops", 2))
        max_edges = int(self.ctx.options.get("graphrag_max_edges", 120))
        seeds = self.find_seeds(q)
        if not seeds or q.family == "localization":
            seeds += [s for s in self.vector_seeds(q) if s not in seeds]
        blank = q.meta.get("blank")
        edges: list[str] = []
        nodes: list[str] = []
        snippets: list[str] = []
        for seed in seeds[:4]:
            e, n, s = self.expand(seed, hops, blank, max_edges - len(edges))
            edges = list(dict.fromkeys(edges + e))  # paths share hops: keep each edge once, in order
            nodes += n
            snippets += s
            if len(edges) >= max_edges:
                break
        parts = []
        if seeds:
            parts.append("Symbols: " + ", ".join(seeds[:4]))
        if edges:
            parts.append("Observed graph edges (resolved_by: " + "/".join(PRECISE_RESOLVERS) +
                         " are precise static bindings, name is a guess). "
                         "Missing edges do not prove a relationship cannot occur at runtime:\n" + "\n".join(edges))
        parts += snippets
        context = clip("\n\n".join(parts), MAX_CONTEXT_CHARS)
        # Only what survived truncation was shown to the model; context recall must not count the rest.
        shown = [n for n in dict.fromkeys(nodes) if n and n in context]
        return Retrieval(context, shown)

    def expand(self, seed: str, hops: int, blank: str | None, budget: int) -> tuple[list[str], list[str], list[str]]:
        store, b = self.ctx.store, self.ctx.build_id
        edges: list[str] = []
        nodes: list[str] = [seed]
        snippets: list[str] = []
        is_class = seed in self.ctx.index.classes
        label = "Class" if is_class else "Function"
        info = store.read(f"MATCH (n:{label} {{build_id: $b, qualified_name: $qn}}) RETURN properties(n) AS p",
                          {"b": b, "qn": seed})
        if not info:
            return edges, nodes, snippets
        p = info[0]["p"]
        nodes.append(p["file_path"])
        edges.append(f"{p['file_path']} -DEFINES-> {seed}")
        hops = max(1, min(hops, 3))

        if not is_class:
            for r in store.read(
                    f"MATCH path = (s:Function {{build_id: $b, qualified_name: $qn}})<-[:CALLS*1..{hops}]-(:Function) "
                    "RETURN [n IN nodes(path) | n.qualified_name] AS chain, "
                    "[r IN relationships(path) | coalesce(r.resolved_by, '') + ', line ' + toString(r.line)] AS how "
                    "LIMIT $limit", {"b": b, "qn": seed, "limit": budget}):
                chain, how = r["chain"], r["how"]
                for i in range(len(how)):
                    edges.append(f"{chain[i + 1]} -CALLS-> {chain[i]} [{how[i]}]")
                nodes += chain
            if seed != blank:  # a blanked function has no body, so no outgoing calls
                for r in store.read(
                        "MATCH (:Function {build_id: $b, qualified_name: $qn})-[r:CALLS]->(c:Function) "
                        "RETURN c.qualified_name AS callee, c.signature AS sig, r.resolved_by AS by, r.line AS line "
                        "LIMIT 40", {"b": b, "qn": seed}):
                    edges.append(f"{seed} -CALLS-> {r['callee']} [{r['by']}, line {r['line']}]")
                    nodes.append(r["callee"])
            if p.get("class_qn"):
                edges.append(f"{p['class_qn']} -DEFINES-> {seed}")
                nodes.append(p["class_qn"])
                siblings = [f"  {r['qn']}: {r['sig']}" for r in store.read(
                    "MATCH (:Class {build_id: $b, qualified_name: $c})-[:DEFINES]->(m:Function) "
                    "RETURN m.qualified_name AS qn, m.signature AS sig ORDER BY m.start_line LIMIT 40",
                    {"b": b, "c": p["class_qn"]}) if r["qn"] != seed]
                if siblings:
                    snippets.append(f"Other methods of {p['class_qn']}:\n" + "\n".join(siblings))
            for r in store.read(
                    "MATCH (a:Author)-[:AUTHORED]->(c:Commit)-[:MODIFIES]->(:Function {build_id: $b, qualified_name: $qn}) "
                    "OPTIONAL MATCH (pr:PullRequest)-[:INCLUDES]->(c) "
                    "RETURN c.short_sha AS sha, a.name AS author, c.committed_at AS at, c.title AS title, "
                    "collect(pr.number) AS prs ORDER BY at DESC LIMIT 3", {"b": b, "qn": seed}):
                prs = "".join(f" (PR #{n})" for n in r["prs"] if n is not None)
                edges.append(f"commit:{r['sha']} -MODIFIES-> {seed} [by {r['author']} at {r['at']}{prs}: {r['title']}]")
                nodes += [r["author"], *[f"#{n}" for n in r["prs"] if n is not None]]
            if seed != blank:
                snippets.append(code_chunk(seed, p["file_path"], p.get("start_line"), p.get("end_line"), p.get("code"),
                                           p.get("language")))
            else:
                snippets.append(f"{seed} is the function to write: def {p.get('signature')}")
        else:
            for r in store.read(
                    "MATCH path = (:Class {build_id: $b, qualified_name: $qn})<-[:INHERITS*1..]-(:Class) "
                    "RETURN [n IN nodes(path) | n.qualified_name] AS chain, "
                    "[r IN relationships(path) | r.resolved_by] AS how LIMIT $limit",
                    {"b": b, "qn": seed, "limit": budget}):
                for i in range(len(r["how"])):
                    edges.append(f"{r['chain'][i + 1]} -INHERITS-> {r['chain'][i]} [{r['how'][i]}]")
                nodes += r["chain"]
            for r in store.read(
                    "MATCH (:Class {build_id: $b, qualified_name: $qn})-[r:INHERITS]->(base:Class) "
                    "RETURN base.qualified_name AS base, r.resolved_by AS how", {"b": b, "qn": seed}):
                edges.append(f"{seed} -INHERITS-> {r['base']} [{r['how']}]")
                nodes.append(r["base"])
            for r in store.read(
                    "MATCH (:Class {build_id: $b, qualified_name: $qn})-[:DEFINES]->(m:Function) "
                    "RETURN m.qualified_name AS qn, m.signature AS sig LIMIT 40", {"b": b, "qn": seed}):
                edges.append(f"{seed} -DEFINES-> {r['qn']}")
                nodes.append(r["qn"])
            if p.get("external_bases"):
                snippets.append(f"{seed} also has external bases: {', '.join(p['external_bases'])}")
        return list(dict.fromkeys(edges))[:budget], nodes, snippets
