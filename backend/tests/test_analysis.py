"""Change impact and the symbol checker's self-audit, on the fixture repo's hand-checked graph."""

from app.analysis.impact import analyze_impact

SCALE_DIFF = """\
--- a/src/minipkg/utils.py
+++ b/src/minipkg/utils.py
@@ -13,2 +13,2 @@ def normalize(x):
 def scale(x, factor=2):
-    return x * factor
+    return factor * x
"""


def names(rows):
    return {r["qualified_name"]: r["hops"] for r in rows}


def test_impact_of_a_diff_follows_precise_callers_and_finds_tests(mini_index):
    report = analyze_impact(mini_index, diff=SCALE_DIFF, max_hops=3)
    assert [c["qualified_name"] for c in report.changed] == ["minipkg.utils.scale"]
    assert names(report.affected) == {"minipkg.core.Engine.run": 1, "minipkg.core.TurboEngine.run": 2,
                                      "minipkg.core.run": 2}
    assert names(report.tests) == {"test_core.test_scale": 1, "test_core.test_run": 3,
                                   "test_core.test_run_reexport": 3, "test_core.TestEngine.test_turbo": 3}
    assert {t["pytest_id"] for t in report.tests} >= {"tests/test_core.py::test_scale",
                                                     "tests/test_core.py::TestEngine::test_turbo"}
    # the same impact set as the impact family's 3-hop gold answer for `scale`
    assert names(analyze_impact(mini_index, functions=["scale"], max_hops=1).tests) == {"test_core.test_scale": 1}


def test_impact_lists_name_matched_callers_separately(mini_index):
    report = analyze_impact(mini_index, functions=["BaseEngine.start"], max_hops=1)
    assert names(report.affected) == {"minipkg.core.Engine.run": 1}
    assert names(report.possibly_affected) == {"minipkg.core.restart": 1}  # `engine.start()` on an untyped param
    md = report.to_markdown(unresolved_call_rate=0.1)
    assert "`minipkg.core.Engine.run` | 1" in md and "Possibly affected (1" in md and "10.0%" in md


def test_impact_reports_inputs_it_cannot_map(mini_index):
    docstring_only = "--- a/src/minipkg/utils.py\n+++ b/src/minipkg/utils.py\n@@ -1,1 +1,1 @@\n-\"\"\"Small.\"\"\"\n+\"\"\"Tiny.\"\"\"\n"
    report = analyze_impact(mini_index, diff=docstring_only, functions=["no_such_function"])
    assert report.changed == [] and len(report.unmatched) == 2
    assert "No function of this build is changed" in report.to_markdown()


def test_symbol_checker_self_audit_on_the_fixture(mini_graph, mini_index):
    from app.analysis.symbol_audit import symbol_self_audit

    class Store:  # serves the fixture's functions the way GraphStore.read would
        def read(self, _cypher, _params):
            return [{"qn": f.qualified_name, "module": f.module, "class_qn": f.class_qn, "code": f.code}
                    for f in mini_graph.functions.values()
                    if not mini_graph.files[f.file_path].is_test and not f.is_nested]

    audit = symbol_self_audit(Store(), 1, mini_index)
    assert audit["functions_checked"] == 15 and audit["functions_flagged"] == 0, audit["examples"]
