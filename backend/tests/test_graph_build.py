"""The fixture repo's graph is known by hand; these tests pin it down for both resolver chains."""

from app.graph.scip import Occurrence, ScipDocument, decode_index, encode_index

EXPECTED_PRECISE_CALLS = {
    ("minipkg.utils.normalize", "minipkg.utils.helper"),
    ("minipkg.utils.compute", "minipkg.utils.helper"),
    ("minipkg.core.BaseEngine.start", "minipkg.core.BaseEngine._prepare"),  # self call
    ("minipkg.core.BaseEngine._prepare", "minipkg.utils.helper"),  # from-import
    ("minipkg.core.Engine.run", "minipkg.utils.normalize"),  # aliased import (norm)
    ("minipkg.core.Engine.run", "minipkg.core.BaseEngine.start"),  # inherited self method
    ("minipkg.core.Engine.run", "minipkg.utils.scale"),  # module attribute (utils.scale)
    ("minipkg.core.Engine.build", "minipkg.core.Engine.__init__"),  # instantiation
    ("minipkg.core.TurboEngine.run", "minipkg.core.Engine.run"),  # super()
    ("minipkg.core.run", "minipkg.core.Engine.build"),  # Class.staticmethod
    ("test_core.test_run", "minipkg.core.run"),
    ("test_core.test_run_reexport", "minipkg.core.run"),  # re-exported from the package __init__
    ("test_core.test_scale", "minipkg.utils.scale"),
    ("test_core.TestEngine.test_turbo", "minipkg.core.Engine.__init__"),  # inherited constructor
    ("test_core.TestEngine.test_turbo", "minipkg.core.TurboEngine.run"),  # Class(...).method
}


def precise(graph):
    return {k for k, e in graph.calls.items() if e.resolved_by in ("scip", "jedi", "import")}


# `engine = Engine.build("default"); engine.run(x)`: Engine.build returns Engine(...), so run -> Engine.run
INFERRED = ("minipkg.core.run", "minipkg.core.Engine.run")
# `def restart(engine): engine.start()` - an untyped parameter, so only a bare-name match is possible
NAME_EDGE = ("minipkg.core.restart", "minipkg.core.BaseEngine.start")


def test_jedi_chain_resolves_precisely(mini_graph):
    assert precise(mini_graph) == EXPECTED_PRECISE_CALLS | {INFERRED}
    assert all(e.resolved_by == "jedi" for k, e in mini_graph.calls.items() if k != NAME_EDGE)
    assert mini_graph.calls[NAME_EDGE].resolved_by == "name"


def test_import_chain_matches_jedi(mini_graph_import_only):
    g = mini_graph_import_only
    assert precise(g) == EXPECTED_PRECISE_CALLS | {INFERRED}
    assert g.functions["minipkg.core.Engine.build"].returns == "minipkg.core.Engine"
    name_edges = {k: e.ambiguity for k, e in g.calls.items() if e.resolved_by == "name"}
    assert name_edges == {NAME_EDGE: 1}


def test_unresolved_and_stats(mini_graph):
    assert dict(mini_graph.unresolved) == {"minipkg.core.dispatch": ["handlers[key]"],
                                           "minipkg.utils.cached.<locals>.wrapper": ["fn"]}
    s = mini_graph.stats
    assert s["call_sites"] == 20 and s["external"] == 1  # super() is a builtin call
    assert s["unresolved"] == 1 and s["dynamic"] == 1
    assert s["unresolved_call_rate"] == round(2 / 19, 4) and s["name_only_rate"] == round(1 / 19, 4)


def test_inherits_tests_and_imports(mini_graph):
    assert mini_graph.inherits == {("minipkg.core.Engine", "minipkg.core.BaseEngine"): "import",
                                   ("minipkg.core.TurboEngine", "minipkg.core.Engine"): "import"}
    assert set(mini_graph.tests) == {
        ("test_core.test_run", "minipkg.core.run"), ("test_core.test_run_reexport", "minipkg.core.run"),
        ("test_core.test_scale", "minipkg.utils.scale"),
        ("test_core.TestEngine.test_turbo", "minipkg.core.Engine.__init__"),
        ("test_core.TestEngine.test_turbo", "minipkg.core.TurboEngine.run")}
    assert ("tests/test_core.py", "module", "minipkg.core") in mini_graph.file_imports
    test = mini_graph.functions["test_core.TestEngine.test_turbo"]
    assert test.is_test and test.pytest_id == "tests/test_core.py::TestEngine::test_turbo"


def test_namespace_follows_aliases(mini_graph):
    ns = mini_graph.files["src/minipkg/core.py"].namespace
    assert ns["norm"] == ["function", "minipkg.utils.normalize"]
    assert ns["utils"] == ["module", "minipkg.utils"]
    assert mini_graph.files["src/minipkg/__init__.py"].namespace["run"] == ["function", "minipkg.core.run"]


def test_scip_round_trip_and_resolution(mini_graph):
    from app.graph.resolver import ScipResolver
    from app.graph.symbols import SymbolTable

    helper_def = Occurrence(3, 4, 10, "scip-python python mini 0.1 minipkg/utils/helper().", 1)
    helper_ref = Occurrence(9, 11, 17, "scip-python python mini 0.1 minipkg/utils/helper().", 0)
    external = Occurrence(13, 15, 18, "scip-python python python-stdlib 3.11 builtins/len().", 0)
    docs = [ScipDocument("src/minipkg/utils.py", [helper_def, helper_ref, external])]
    data = encode_index(docs)
    decoded = decode_index(data)
    assert decoded[0].path == "src/minipkg/utils.py" and decoded[0].occurrences[1].start == 11

    resolver = ScipResolver(None, SymbolTable(mini_graph), data)
    site = next(s for s in mini_graph.call_sites if s.caller == "minipkg.utils.normalize")
    fn = mini_graph.functions[site.caller]
    assert (site.line, site.col) == (10, 11)
    res = resolver.resolve(site, fn)
    assert res.status == "resolved" and res.targets == ["minipkg.utils.helper"] and res.by == "scip"


def test_self_referential_assignment_does_not_recurse(tmp_path):
    from app.graph.builder import build_structure

    (tmp_path / "m.py").write_text(
        "def f(headers):\n    headers = headers.copy()\n    return headers.get('a')\n\n"
        "def g():\n    a = b.x()\n    b = a.y()\n    return a.z()\n")
    g = build_structure(tmp_path, "m", str(tmp_path), None, ["import", "name"])
    assert g.stats["call_sites"] == 5
