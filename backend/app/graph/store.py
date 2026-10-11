"""Neo4j access: write a CodeGraph for one build, and read it back for test generation, retrieval and scoring."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Iterable, Iterator
from functools import lru_cache
from pathlib import Path

from neo4j import READ_ACCESS, GraphDatabase, unit_of_work
from neo4j.exceptions import Neo4jError

from app.config import get_settings
from app.graph.model import CodeGraph

log = logging.getLogger(__name__)

LABELS = ("Repo", "Module", "File", "Class", "Function", "ExternalPackage", "Author", "Commit",
          "PullRequest", "Issue", "Feature")
BATCH = 2000
VECTOR_INDEX = "function_code_embedding"
SUMMARY_VECTOR_INDEX = "function_summary_embedding"
VECTOR_PROPERTIES = {VECTOR_INDEX: "code_embedding", SUMMARY_VECTOR_INDEX: "summary_embedding"}
_WRITE_CLAUSES = re.compile(
    r"\b(CREATE|MERGE|DELETE|DETACH|SET|REMOVE|DROP|LOAD\s+CSV|FOREACH|GRANT|DENY|REVOKE)\b"
    r"|CALL\s+(dbms|db\.create|db\.index\.vector\.set|apoc\.(create|merge|refactor|periodic|load|export|cypher\.run))",
    re.IGNORECASE,
)


# Look a symbol up by qualified name through the per-label indexes.
_SYMBOL = ("CALL { MATCH (n:Function {build_id: $b, qualified_name: $qn}) RETURN n "
           "UNION MATCH (n:Class {build_id: $b, qualified_name: $qn}) RETURN n }")


def uid(build_id: int, kind: str, key: str | int) -> str:
    return f"{build_id}:{kind}:{key}"


def _batched(rows: list[dict], size: int = BATCH) -> Iterator[list[dict]]:
    for i in range(0, len(rows), size):
        yield rows[i:i + size]


def is_read_only(cypher: str) -> bool:
    """Reject anything that could write. Queries also run in READ transactions, which Neo4j enforces."""
    stripped = re.sub(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"|//[^\n]*", "''", cypher)
    return _WRITE_CLAUSES.search(stripped) is None


class GraphStore:
    def __init__(self, uri: str, user: str, password: str, database: str = "neo4j") -> None:
        # Notifications off: the classic `CALL { WITH n ... }` form we use for 5.15 compatibility is
        # reported as deprecated by 5.23+, which would flood the logs.
        self.driver = GraphDatabase.driver(uri, auth=(user, password), notifications_min_severity="OFF")
        self.database = database

    def close(self) -> None:
        self.driver.close()

    def verify(self) -> None:
        self.driver.verify_connectivity()

    # -- generic access ----------------------------------------------------------------------------------

    def read(self, cypher: str, params: dict | None = None, timeout: float | None = None) -> list[dict]:
        @unit_of_work(timeout=timeout)
        def work(tx):
            return [record.data() for record in tx.run(cypher, params or {})]

        with self.driver.session(database=self.database, default_access_mode=READ_ACCESS) as session:
            return session.execute_read(work)

    def read_scoped(self, cypher: str, build_id: int, timeout: float = 15) -> list[dict]:
        """Execute a restricted agent query with an enforced build filter on each matched node."""
        from app.graph.query_scope import scope_query

        return self.read(scope_query(cypher), {"build_id": build_id}, timeout=timeout)

    def write(self, cypher: str, params: dict | None = None) -> None:
        def work(tx):
            tx.run(cypher, params or {}).consume()

        with self.driver.session(database=self.database) as session:
            session.execute_write(work)

    def run_autocommit(self, cypher: str, params: dict | None = None) -> None:
        with self.driver.session(database=self.database) as session:
            session.run(cypher, params or {}).consume()

    def ensure_schema(self) -> None:
        text = (Path(__file__).parent / "schema.cypher").read_text()
        statements = [s.strip() for s in re.sub(r"//[^\n]*", "", text).split(";") if s.strip()]
        for statement in statements:
            self.run_autocommit(statement)

    def ensure_vector_index(self, name: str, prop: str, dim: int) -> None:
        existing = self.read("SHOW INDEXES YIELD name, type, options WHERE type = 'VECTOR' RETURN name, options")
        for row in existing:
            if row["name"] == name:
                current = row["options"]["indexConfig"]["vector.dimensions"]
                if int(current) != dim:
                    raise RuntimeError(
                        f"Vector index {name} has dimension {current} but the embedding model produces {dim}. "
                        f"Use the same embedding model for every build, or run `DROP INDEX {name}`.")
                return
        self.run_autocommit(
            f"CREATE VECTOR INDEX {name} IF NOT EXISTS FOR (f:Function) ON (f.{prop}) "
            f"OPTIONS {{indexConfig: {{`vector.dimensions`: {int(dim)}, `vector.similarity_function`: 'cosine'}}}}")

    # -- writing a build ---------------------------------------------------------------------------------

    def delete_build(self, build_id: int) -> None:
        for label in LABELS:
            self.run_autocommit(
                f"MATCH (n:{label} {{build_id: $b}}) CALL {{ WITH n DETACH DELETE n }} IN TRANSACTIONS OF 5000 ROWS",
                {"b": build_id})

    def _create_nodes(self, label: str, rows: list[dict]) -> None:
        for batch in _batched(rows):
            self.write(f"UNWIND $rows AS row CREATE (n:{label}) SET n = row", {"rows": batch})

    def _create_rels(self, src_label: str, rel: str, dst_label: str, rows: list[dict]) -> None:
        cypher = (f"UNWIND $rows AS row MATCH (a:{src_label} {{uid: row.src}}) MATCH (b:{dst_label} {{uid: row.dst}}) "
                  f"CREATE (a)-[r:{rel}]->(b) SET r = row.props")
        for batch in _batched(rows):
            self.write(cypher, {"rows": batch})

    def write_graph(self, build_id: int, g: CodeGraph, progress: Callable[[str], None] | None = None) -> None:
        """Replace everything stored for `build_id` with the given graph."""
        b = build_id
        say = progress or (lambda _m: None)
        self.ensure_schema()
        existing = self.read("MATCH (r:Repo {build_id: $b}) RETURN r.url AS url", {"b": b})
        if existing and existing[0]["url"] != g.repo_url:
            # Graph keys are random (GraphBuild.graph_id), so this only happens with builds created before
            # graph keys existed, when a second results database reused the same row ids.
            raise RuntimeError(
                f"Neo4j already holds build_id {b} for {existing[0]['url']!r}, not {g.repo_url!r}; refusing to "
                f"overwrite another repository's graph. Remove it with `MATCH (n {{build_id: {b}}}) DETACH DELETE n` "
                f"if it is no longer needed.")
        say("writing graph: clearing previous data")
        self.delete_build(b)

        self._create_nodes("Repo", [{"uid": uid(b, "repo", g.repo_name), "build_id": b, "name": g.repo_name,
                                      "url": g.repo_url, "commit_sha": g.commit_sha}])
        self._create_nodes("Module", [{"uid": uid(b, "mod", m), "build_id": b, "name": m, "path": p,
                                        "is_package": g.files[p].is_package} for m, p in g.modules.items()])
        self._create_nodes("File", [{
            "uid": uid(b, "file", f.path), "build_id": b, "path": f.path, "module": f.module, "is_test": f.is_test,
            "is_package": f.is_package, "loc": f.loc, "sha1": f.sha1, "parse_errors": f.parse_errors,
            "language": f.language,
            "analysis_level": f.analysis_level,
            "namespace_json": json.dumps(f.namespace, sort_keys=True),
        } for f in g.files.values()])
        self._create_nodes("Class", [{
            "uid": uid(b, "cls", c.qualified_name), "build_id": b, "qualified_name": c.qualified_name,
            "name": c.name, "module": c.module, "file_path": c.file_path, "bases": c.bases,
            "language": c.language,
            "external_bases": list(g.external_bases.get(c.qualified_name, [])), "decorators": c.decorators,
            "start_line": c.start_line, "end_line": c.end_line, "docstring": c.docstring, "is_test": c.is_test,
            "is_nested": c.is_nested, "attributes": sorted(c.attributes),
            "in_test_file": g.files[c.file_path].is_test,
        } for c in g.classes.values()])
        say(f"writing graph: {len(g.functions)} functions")
        self._create_nodes("Function", [{
            "uid": uid(b, "fn", f.qualified_name), "build_id": b, "qualified_name": f.qualified_name,
            "name": f.name, "module": f.module, "file_path": f.file_path, "class_qn": f.class_qn,
            "language": f.language,
            "signature": f.signature, "params_json": json.dumps([p.to_dict() for p in f.params]),
            "param_names": [p.name for p in f.params], "decorators": f.decorators, "start_line": f.start_line,
            "def_line": f.name_line,
            "end_line": f.end_line, "docstring": f.docstring, "code": f.code, "is_test": f.is_test,
            "is_method": f.is_method, "is_nested": f.is_nested, "is_async": f.is_async, "kind": f.kind,
            "pytest_id": f.pytest_id, "returns": f.returns, "in_test_file": g.files[f.file_path].is_test,
            "unresolved_calls": sorted(set(g.unresolved.get(f.qualified_name, []))),
        } for f in g.functions.values()])
        self._create_nodes("ExternalPackage", [{"uid": uid(b, "ext", name), "build_id": b, "name": name,
                                                 "is_stdlib": is_std} for name, is_std in g.external_packages.items()])

        repo_uid = uid(b, "repo", g.repo_name)
        self._create_rels("Repo", "CONTAINS", "Module",
                          [{"src": repo_uid, "dst": uid(b, "mod", m), "props": {}} for m in g.modules])
        self._create_rels("Module", "CONTAINS", "File",
                          [{"src": uid(b, "mod", m), "dst": uid(b, "file", p), "props": {}} for m, p in g.modules.items()])

        defines: dict[tuple[str, str], list[dict]] = {}

        def parent_of(file_path: str, nested_in_class: str | None, nested_in_fn: str | None) -> tuple[str, str]:
            if nested_in_fn:
                return "Function", uid(b, "fn", nested_in_fn)
            if nested_in_class:
                return "Class", uid(b, "cls", nested_in_class)
            return "File", uid(b, "file", file_path)

        for c in g.classes.values():
            outer = c.qualified_name.rpartition(".")[0]
            in_fn = outer.removesuffix(".<locals>") if outer.endswith(".<locals>") else None
            in_cls = outer if outer in g.classes else None
            label, src = parent_of(c.file_path, in_cls, in_fn if in_fn in g.functions else None)
            defines.setdefault((label, "Class"), []).append({"src": src, "dst": uid(b, "cls", c.qualified_name), "props": {}})
        for f in g.functions.values():
            label, src = parent_of(f.file_path, f.class_qn, f.parent_qn)
            defines.setdefault((label, "Function"), []).append({"src": src, "dst": uid(b, "fn", f.qualified_name), "props": {}})
        for (src_label, dst_label), rows in defines.items():
            self._create_rels(src_label, "DEFINES", dst_label, rows)

        say(f"writing graph: {len(g.calls)} CALLS edges")
        self._create_rels("Function", "CALLS", "Function", [{
            "src": uid(b, "fn", e.caller), "dst": uid(b, "fn", e.callee),
            "props": {"line": min(e.lines), "lines": sorted(e.lines), "resolved_by": e.resolved_by,
                      "ambiguity": e.ambiguity},
        } for e in g.calls.values()])
        imports_mod, imports_ext = [], []
        for (path, kind, target), line in g.file_imports.items():
            row = {"src": uid(b, "file", path), "dst": uid(b, "mod" if kind == "module" else "ext", target),
                   "props": {"line": line}}
            (imports_mod if kind == "module" else imports_ext).append(row)
        self._create_rels("File", "IMPORTS", "Module", imports_mod)
        self._create_rels("File", "IMPORTS", "ExternalPackage", imports_ext)
        position = {}
        for c in g.classes.values():
            for i, base in enumerate(c.bases):
                position[(c.qualified_name, base.split(".")[-1])] = i
        self._create_rels("Class", "INHERITS", "Class", [{
            "src": uid(b, "cls", child), "dst": uid(b, "cls", parent),
            "props": {"resolved_by": by, "position": position.get((child, parent.rsplit(".", 1)[-1]), 0)},
        } for (child, parent), by in g.inherits.items()])
        self._create_rels("Function", "TESTS", "Function", [{
            "src": uid(b, "fn", t), "dst": uid(b, "fn", f), "props": {"resolved_by": by},
        } for (t, f), by in g.tests.items()])
        self.write_history(b, g, say)
        say("writing graph: done")

    def write_history(self, b: int, g: CodeGraph, say: Callable[[str], None]) -> None:
        if not g.commits:
            return
        say(f"writing history: {len(g.commits)} commits, {len(g.pull_requests)} PRs, {len(g.issues)} issues")
        authors: dict[str, dict] = {}
        for c in sorted(g.commits.values(), key=lambda c: c.order, reverse=True):  # newest name wins
            email = c.author_email.lower()
            authors[email] = {"uid": uid(b, "author", email), "build_id": b, "name": c.author_name, "email": email,
                              "login": g.author_logins.get(email)}
        self._create_nodes("Author", list(authors.values()))
        self._create_nodes("Commit", [{
            "uid": uid(b, "commit", c.sha), "build_id": b, "sha": c.sha, "short_sha": c.sha[:8], "title": c.title,
            "message": c.message, "authored_at": c.authored_at, "committed_at": c.committed_at, "order": c.order,
            "is_merge": c.is_merge, "author_name": c.author_name,
        } for c in g.commits.values()])
        self._create_rels("Author", "AUTHORED", "Commit", [{
            "src": uid(b, "author", c.author_email.lower()), "dst": uid(b, "commit", c.sha), "props": {},
        } for c in g.commits.values()])
        self._create_rels("Commit", "MODIFIES", "Function", [{
            "src": uid(b, "commit", sha), "dst": uid(b, "fn", fn), "props": props,
        } for (sha, fn), props in g.modifies.items() if fn in g.functions])
        self._create_nodes("PullRequest", [{
            "uid": uid(b, "pr", pr.number), "build_id": b, "number": pr.number, "title": pr.title, "body": pr.body,
            "author": pr.author, "merged_at": pr.merged_at, "url": pr.url, "source": pr.source,
        } for pr in g.pull_requests.values()])
        self._create_rels("PullRequest", "INCLUDES", "Commit", [{
            "src": uid(b, "pr", pr.number), "dst": uid(b, "commit", sha), "props": {},
        } for pr in g.pull_requests.values() for sha in sorted(pr.commits) if sha in g.commits])
        self._create_nodes("Issue", [{
            "uid": uid(b, "issue", i.number), "build_id": b, "number": i.number, "title": i.title, "body": i.body,
            "state": i.state, "created_at": i.created_at, "url": i.url, "labels": i.labels,
        } for i in g.issues.values()])
        self._create_rels("PullRequest", "FIXES", "Issue", [{
            "src": uid(b, "pr", pr.number), "dst": uid(b, "issue", n), "props": {"source": pr.source},
        } for pr in g.pull_requests.values() for n in sorted(pr.fixes) if n in g.issues])

    # -- reads used across the app -----------------------------------------------------------------------

    def counts(self, build_id: int) -> dict:
        """Node counts per label and edge counts per type, going through the per-label build_id indexes."""
        nodes: dict[str, int] = {}
        edges: dict[str, int] = {}
        for label in LABELS:
            n = self.read(f"MATCH (n:{label} {{build_id: $b}}) RETURN count(n) AS n", {"b": build_id})[0]["n"]
            if not n:
                continue
            nodes[label] = n
            for r in self.read(f"MATCH (:{label} {{build_id: $b}})-[r]->() RETURN type(r) AS type, count(r) AS n",
                               {"b": build_id}):
                edges[r["type"]] = edges.get(r["type"], 0) + r["n"]
        return {"nodes": nodes, "edges": edges}

    def search_symbols(self, build_id: int, text: str, limit: int = 20) -> list[dict]:
        return self.read(
            "MATCH (n) WHERE (n:Function OR n:Class) AND n.build_id = $b "
            "AND (toLower(n.qualified_name) CONTAINS toLower($q)) "
            "RETURN n.qualified_name AS qualified_name, labels(n)[0] AS kind, n.file_path AS file_path, "
            "n.start_line AS start_line, n.end_line AS end_line, coalesce(n.is_test, false) AS is_test "
            "ORDER BY size(n.qualified_name) LIMIT $limit",
            {"b": build_id, "q": text, "limit": limit})

    def node_details(self, build_id: int, qualified_name: str) -> dict | None:
        rows = self.read(
            f"{_SYMBOL} RETURN labels(n)[0] AS kind, properties(n) AS props", {"b": build_id, "qn": qualified_name})
        if not rows:
            return None
        props = {k: v for k, v in rows[0]["props"].items() if not k.endswith("embedding")}
        return {"kind": rows[0]["kind"], **props}

    def neighborhood(self, build_id: int, qualified_name: str, depth: int = 1, limit: int = 150) -> dict:
        """Nodes and edges within `depth` hops of a symbol, for the graph explorer."""
        depth = max(1, min(int(depth), 3))
        rows = self.read(
            f"{_SYMBOL} WITH n AS s "
            f"MATCH p = (s)-[:CALLS|INHERITS|TESTS|DEFINES*1..{depth}]-(n) WHERE n:Function OR n:Class "
            f"WITH p LIMIT $limit UNWIND relationships(p) AS r "
            f"WITH DISTINCT r RETURN startNode(r).qualified_name AS source, endNode(r).qualified_name AS target, "
            f"type(r) AS type, r.resolved_by AS resolved_by, r.line AS line",
            {"b": build_id, "qn": qualified_name, "limit": limit})
        names = {qualified_name} | {r["source"] for r in rows} | {r["target"] for r in rows}
        nodes = self.read(
            "MATCH (n) WHERE (n:Function OR n:Class) AND n.build_id = $b AND n.qualified_name IN $names "
            "RETURN n.qualified_name AS id, labels(n)[0] AS kind, n.name AS name, n.file_path AS file_path, "
            "n.start_line AS start_line, n.end_line AS end_line, coalesce(n.is_test, false) AS is_test",
            {"b": build_id, "names": [n for n in names if n]})
        return {"nodes": nodes, "edges": [r for r in rows if r["source"] and r["target"]]}

    def load_index_rows(self, build_id: int) -> dict[str, list[dict]]:
        """Everything the eval-time NameIndex needs, in one round of queries."""
        functions = self.read(
            "MATCH (f:Function {build_id: $b}) RETURN f.qualified_name AS qualified_name, f.name AS name, "
            "f.module AS module, f.file_path AS file_path, f.class_qn AS class_qn, f.params_json AS params_json, "
            "f.kind AS kind, f.is_test AS is_test, f.is_nested AS is_nested, f.start_line AS start_line, "
            "f.end_line AS end_line, f.returns AS returns, f.unresolved_calls AS unresolved_calls, "
            "f.pytest_id AS pytest_id, f.language AS language", {"b": build_id})
        classes = self.read(
            "MATCH (c:Class {build_id: $b}) OPTIONAL MATCH (c)-[:INHERITS]->(p:Class) "
            "RETURN c.qualified_name AS qualified_name, c.name AS name, c.module AS module, "
            "c.file_path AS file_path, c.external_bases AS external_bases, c.attributes AS attributes, "
            "c.language AS language, collect(p.qualified_name) AS bases", {"b": build_id})
        files = self.read(
            "MATCH (f:File {build_id: $b}) RETURN f.path AS path, f.module AS module, f.is_test AS is_test, "
            "f.namespace_json AS namespace_json, f.language AS language, f.analysis_level AS analysis_level", {"b": build_id})
        calls = self.read(
            "MATCH (a:Function {build_id: $b})-[r:CALLS]->(c:Function) RETURN a.qualified_name AS caller, "
            "c.qualified_name AS callee, r.resolved_by AS resolved_by, r.line AS line", {"b": build_id})
        inherits = self.read(
            "MATCH (a:Class {build_id: $b})-[r:INHERITS]->(c:Class) RETURN a.qualified_name AS child, "
            "c.qualified_name AS parent, r.resolved_by AS resolved_by", {"b": build_id})
        modifies = self.read(
            "MATCH (a:Author {build_id: $b})-[:AUTHORED]->(:Commit)-[:MODIFIES]->(f:Function) "
            "RETURN DISTINCT f.qualified_name AS function, a.name AS name, a.email AS email, a.login AS login",
            {"b": build_id})
        return {"functions": functions, "classes": classes, "files": files, "calls": calls, "inherits": inherits,
                "modifies": modifies}

    def set_function_props(self, build_id: int, rows: Iterable[dict]) -> None:
        """rows: {"qualified_name": ..., "props": {...}}"""
        rows = list(rows)
        for batch in _batched(rows, 500):
            self.write("UNWIND $rows AS row MATCH (f:Function {build_id: $b, qualified_name: row.qualified_name}) "
                       "SET f += row.props", {"b": build_id, "rows": batch})

    def vector_search(self, build_id: int, embedding: list[float], k: int = 8,
                      index: str = VECTOR_INDEX) -> list[dict]:
        """k nearest functions of this build.

        One vector index spans every build, so it is over-fetched and filtered to this build. When other builds
        crowd this one out of the nearest neighbours (many builds, or near-duplicate code across commits), fall
        back to an exact cosine scan of this build's functions, so retrieval never silently returns too little.
        """
        columns = ("node.qualified_name AS qualified_name, node.file_path AS file_path, "
                   "node.start_line AS start_line, node.end_line AS end_line, node.code AS code, "
                   "node.signature AS signature, node.summary AS summary")
        params = {"index": index, "vec": embedding, "b": build_id, "k": k}
        for fetch in (max(k * 25, 250), 5000):
            rows = self.read(
                "CALL db.index.vector.queryNodes($index, $fetch, $vec) YIELD node, score "
                f"WHERE node.build_id = $b RETURN {columns}, score ORDER BY score DESC LIMIT $k",
                {**params, "fetch": fetch})
            if len(rows) >= k:
                return rows
        prop = VECTOR_PROPERTIES.get(index, "code_embedding")
        try:
            return self.read(
                f"MATCH (node:Function {{build_id: $b}}) WHERE node.{prop} IS NOT NULL "
                f"WITH node, vector.similarity.cosine(node.{prop}, $vec) AS score "
                f"RETURN {columns}, score ORDER BY score DESC LIMIT $k", params)
        except Neo4jError:  # vector.similarity.cosine needs Neo4j 5.18+: score in Python instead
            import numpy as np

            rows = self.read(f"MATCH (node:Function {{build_id: $b}}) WHERE node.{prop} IS NOT NULL "
                             f"RETURN {columns}, node.{prop} AS vec", params)
            if not rows:
                return []
            matrix = np.asarray([r.pop("vec") for r in rows], dtype=np.float32)
            query = np.asarray(embedding, dtype=np.float32)
            scores = matrix @ query / (np.linalg.norm(matrix, axis=1) * np.linalg.norm(query) + 1e-12)
            for r, s in zip(rows, scores, strict=True):
                r["score"] = (float(s) + 1) / 2  # the same [0, 1] scale as Neo4j's cosine score
            return sorted(rows, key=lambda r: -r["score"])[:k]


@lru_cache
def get_store() -> GraphStore:
    s = get_settings()
    return GraphStore(s.neo4j_uri, s.neo4j_user, s.neo4j_password, s.neo4j_database)
