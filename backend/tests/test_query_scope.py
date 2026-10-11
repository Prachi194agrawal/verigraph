import pytest

from app.graph.query_scope import scope_query


def test_each_match_is_scoped_including_optional_and_anonymous_nodes():
    q = scope_query("MATCH (f:Function)-[:CALLS]->(g) OPTIONAL MATCH (g)<-[:CALLS]-(:Function) RETURN f, g")
    assert q.count("build_id: $build_id") == 4


def test_existing_parameter_scope_and_other_properties_are_preserved():
    q = scope_query("MATCH (f:Function {name: 'helper'})-[:CALLS]->(g {build_id: $build_id}) RETURN g.name")
    assert "name: 'helper', build_id: $build_id" in q
    assert q.count("build_id: $build_id") == 2


def test_strings_comments_and_aggregate_expressions_do_not_change_scoping():
    q = scope_query("MATCH (n) WHERE n.name = 'CALL DELETE' RETURN count(n) // MATCH (other)")
    assert q.count("build_id: $build_id") == 1
    assert "count(n)" in q


@pytest.mark.parametrize("query", [
    "CALL db.labels()", "MATCH (n) DETACH DELETE n", "MATCH (n) RETURN n UNION MATCH (m) RETURN m",
    "MATCH (n {build_id: 99}) RETURN n", "MATCH (n {build_id: $other}) RETURN n",
    "MATCH (n) RETURN [(x)<-[:CALLS]-(y) | x]", "MATCH (n) WHERE EXISTS { MATCH (m) RETURN m } RETURN n",
    "MATCH (n) RETURN n; MATCH (m) RETURN m", "MATCH (n) RETURN [(x)--(y) | x]",
    "MATCH (n) RETURN apoc.cypher.runFirstColumn('MATCH (m) RETURN m', properties(n), false)",
    "MATCH (n) RETURN apoc.cypher.`runFirstColumn`('MATCH (m) RETURN m', properties(n), false)",
])
def test_unsupported_or_cross_build_queries_fail_closed(query):
    with pytest.raises(ValueError):
        scope_query(query)
