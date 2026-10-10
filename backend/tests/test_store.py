from neo4j.exceptions import Neo4jError

from app.graph.store import GraphStore, is_read_only


class _FakeStore(GraphStore):
    """No database: the index queries find nothing, and the server lacks vector.similarity.cosine (< 5.18)."""

    def __init__(self, rows):
        self.rows, self.queries = rows, []

    def read(self, cypher, params=None, timeout=None):
        self.queries.append(cypher)
        if "queryNodes" in cypher:
            return []
        if "vector.similarity.cosine" in cypher:
            raise Neo4jError("Unknown function 'vector.similarity.cosine'")
        return [dict(r) for r in self.rows]


def test_vector_search_exact_fallback_without_cosine_function():
    rows = [{"qualified_name": "a", "vec": [1.0, 0.0]}, {"qualified_name": "b", "vec": [0.6, 0.8]},
            {"qualified_name": "c", "vec": [-1.0, 0.0]}]
    store = _FakeStore(rows)
    found = store.vector_search(1, [0.0, 1.0], k=2)
    assert [r["qualified_name"] for r in found] == ["b", "a"]
    assert abs(found[0]["score"] - 0.9) < 1e-6 and "vec" not in found[0]
    assert sum("queryNodes" in q for q in store.queries) == 2  # widened the over-fetch once before scanning


def test_read_only_guard():
    assert is_read_only("MATCH (f:Function {build_id: $build_id}) RETURN f.name AS n // CREATE nothing")
    assert is_read_only("MATCH (f) WHERE f.name = 'DELETE' RETURN f")
    assert not is_read_only("MATCH (f) DETACH DELETE f")
    assert not is_read_only("CALL apoc.periodic.iterate('MATCH (n) RETURN n', 'DELETE n', {})")
