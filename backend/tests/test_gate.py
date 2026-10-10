from app.cli import check_thresholds

BOARD = [
    {"model": "m", "pipeline": "graphrag", "f1": 0.40, "context_recall": 0.95, "errors": 0,
     "f1_by_family": {"callers": 0.5}},
    {"model": "m", "pipeline": "vector", "f1": 0.20, "context_recall": 0.10, "errors": 2, "f1_by_family": {}},
]


def test_thresholds_floors_ceilings_and_nested_metrics():
    thresholds = {"*": {"errors": {"max": 0}}, "graphrag": {"f1_by_family.callers": {"min": 0.9}},
                  "vector": {"context_recall": {"min": 0.2}}}
    failures = check_thresholds(BOARD, thresholds, baseline=None)
    assert any("m|graphrag: f1_by_family.callers = 0.500 < min 0.9" in f for f in failures)
    assert any("m|vector: errors = 2.000 > max 0" in f for f in failures)
    assert any("m|vector: context_recall = 0.100 < min 0.2" in f for f in failures)
    assert len(failures) == 3


def test_baseline_regression():
    failures = check_thresholds(BOARD, {"max_regression": 0.05}, baseline={"m|graphrag": 0.50, "m|vector": 0.22})
    assert failures == ["m|graphrag: f1 0.400 dropped more than 0.05 below baseline 0.500"]


def test_missing_required_metric_fails_the_gate():
    failures = check_thresholds([{"model": "m", "pipeline": "graphrag", "f1": 0.5}],
                                {"graphrag": {"context_recall": {"min": 0.8}}}, baseline=None)
    assert failures == ["m|graphrag: context_recall is missing"]
