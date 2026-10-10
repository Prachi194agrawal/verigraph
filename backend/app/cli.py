"""Command line: run every step without the API (and the CI eval gate).

    verigraph build https://github.com/psf/requests --commit v2.32.3
    verigraph testgen 1 --per-family 20
    verigraph run 1 --models groq/llama-3.1-8b-instant --pipelines none,vector,graphrag,cypher_agent
    verigraph report 1
"""

from __future__ import annotations

import csv
import json
import logging
import random
import sys
from collections import defaultdict
from pathlib import Path

import typer
import yaml
from rich.console import Console
from rich.markup import escape
from rich.table import Table
from sqlalchemy import func, select

from app import PARSER_VERSION
from app.db import init_db, session_scope
from app.eval.meta_eval import edge_precision, sample_edges
from app.eval.report import compare, hop_curve, leaderboard
from app.graph.store import get_store
from app.ingest.clone import repo_name
from app.models import Answer, Dataset, Experiment, GraphBuild, Question, Repo, SemanticAudit
from app.testgen.templates import FAMILIES
from app.workers.jobs import run_build, run_dataset, run_experiment

app = typer.Typer(add_completion=False, no_args_is_help=True, help="VeriGraph for Code")
console = Console()
FAILED_CALL_RETRIES = 2  # extra passes over questions whose model call failed


def _csv(value: str | None) -> list[str] | None:
    return [v.strip() for v in value.split(",") if v.strip()] if value else None


def _setup(verbose: bool = False) -> None:
    logging.basicConfig(level=logging.INFO if verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("pydriller", "httpx", "LiteLLM", "litellm", "neo4j"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    init_db()


def _graph_id(build_id: int) -> int:
    """The Neo4j key of a build (GraphBuild.graph_id); CLI arguments are always row ids."""
    with session_scope() as db:
        build = db.get(GraphBuild, build_id)
        if build is None:
            console.print(f"[red]build {build_id} not found[/red]")
            raise typer.Exit(1)
        return build.graph_id


def _check(model, row_id: int) -> None:
    with session_scope() as db:
        row = db.get(model, row_id)
        if row.status != "done":
            console.print(f"[red]{model.__tablename__} {row_id} {row.status}[/red]\n{row.error or ''}")
            raise typer.Exit(1)


@app.command("init-db")
def init_db_cmd() -> None:
    """Create the results-database tables and the Neo4j schema."""
    _setup()
    get_store().ensure_schema()
    console.print("ok")


@app.command()
def build(url: str, commit: str = typer.Option(None, help="commit, tag or branch to pin"),
          resolvers: str = typer.Option(None, help="e.g. jedi,import,name (default from RESOLVERS)"),
          history: bool = True, github: bool = True, max_commits: int = typer.Option(None),
          embeddings: bool = True, embedding_model: str = typer.Option(None),
          semantic_model: str = typer.Option(None, help="LLM for summaries and feature tags (skipped if unset)"),
          semantic_limit: int = typer.Option(None), verbose: bool = typer.Option(True)) -> None:
    """Steps 1-3: check out, parse, resolve, mine history, add meaning; write the graph to Neo4j."""
    _setup(verbose)
    options = {"commit_sha": commit, "resolvers": _csv(resolvers), "history": history, "github": github,
               "max_commits": max_commits, "embeddings": embeddings, "embedding_model": embedding_model,
               "semantic_model": semantic_model, "semantic_limit": semantic_limit}
    with session_scope() as db:
        repo = Repo(url=url, name=repo_name(url), commit_sha=commit)
        db.add(repo)
        db.flush()
        b = GraphBuild(repo_id=repo.id, repo=repo.name, parser_version=PARSER_VERSION,
                       options={k: v for k, v in options.items() if v is not None})
        db.add(b)
        db.flush()
        build_id = b.id
    run_build(build_id)
    _check(GraphBuild, build_id)
    with session_scope() as db:
        b = db.get(GraphBuild, build_id)
        stats = b.stats or {}
        console.print(f"[green]build {build_id} done[/green] commit {b.commit_sha} (Neo4j build_id {b.graph_id})")
        console.print_json(json.dumps({k: stats.get(k) for k in (
            "files", "classes", "functions", "test_functions", "call_sites", "by_resolver", "unresolved_call_rate",
            "name_only_rate", "calls_edges", "inherits_edges", "tests_edges", "history", "github", "embeddings")}))


@app.command()
def testgen(build_id: int, families: str = typer.Option(None, help=f"subset of {','.join(FAMILIES)}"),
            per_family: int = 20, seed: int = 0, rephrase_model: str = typer.Option(None),
            version: str = typer.Option(None)) -> None:
    """Step 4: generate questions with Cypher gold answers."""
    _setup()
    with session_scope() as db:
        b = db.get(GraphBuild, build_id)
        ds = Dataset(build_id=build_id, version=version or f"{b.repo.replace('/', '-')}-b{build_id}-s{seed}",
                     options={"families": _csv(families), "per_family": per_family, "seed": seed,
                              "rephrase_model": rephrase_model})
        db.add(ds)
        db.flush()
        dataset_id = ds.id
    run_dataset(dataset_id)
    _check(Dataset, dataset_id)
    with session_scope() as db:
        counts = defaultdict(int)
        for q in db.execute(select(Question).where(Question.dataset_id == dataset_id)).scalars():
            counts[q.family] += 1
    console.print(f"[green]dataset {dataset_id}[/green] {dict(counts)}")


@app.command()
def freeze(dataset_id: int, out: Path, n: int = 50, seed: int = 0) -> None:
    """Write a fixed, family-stratified slice of a dataset as JSONL (for the CI gate)."""
    _setup()
    from app.api.datasets import question_record

    with session_scope() as db:
        questions = list(db.execute(select(Question).where(Question.dataset_id == dataset_id)
                                    .order_by(Question.id)).scalars())
        by_family: dict[str, list[Question]] = defaultdict(list)
        for q in questions:
            by_family[q.family].append(q)
        rng = random.Random(seed)
        for qs in by_family.values():
            rng.shuffle(qs)
        picked: list[Question] = []
        while len(picked) < min(n, len(questions)):  # round-robin over families
            for fam in sorted(by_family):
                if by_family[fam] and len(picked) < n:
                    picked.append(by_family[fam].pop())
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("".join(json.dumps(question_record(q)) + "\n" for q in sorted(picked, key=lambda q: q.id)))
    console.print(f"wrote {len(picked)} questions to {out}")


def load_dataset_file(build_id: int, path: Path, version: str | None = None) -> int:
    records = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    graph_id = _graph_id(build_id)
    with session_scope() as db:
        ds = Dataset(build_id=build_id, version=version or path.stem, status="done", n_questions=len(records),
                     options={"source": str(path)})
        db.add(ds)
        db.flush()
        for r in records:
            params = dict(r.get("gold_params") or {})
            params["build_id"] = graph_id
            db.add(Question(dataset_id=ds.id, dataset_version=ds.version, family=r["family"], text=r["text"],
                            template_text=r.get("template_text", r["text"]), gold_cypher=r.get("gold_cypher", ""),
                            gold_params=params, gold_answer=r["gold_answer"], hop_count=r.get("hop_count"),
                            target=r.get("target"), meta=r.get("meta") or {}))
        return ds.id


@app.command("load-dataset")
def load_dataset(build_id: int, path: Path, version: str = typer.Option(None)) -> None:
    """Import a frozen JSONL question set as a dataset of an existing build."""
    _setup()
    console.print(f"dataset {load_dataset_file(build_id, path, version)}")


@app.command()
def verify(dataset_id: int) -> None:
    """Re-run every gold query against the dataset's build and report questions whose gold has drifted."""
    _setup()
    store = get_store()
    drift, checked = [], 0
    with session_scope() as db:
        ds = db.get(Dataset, dataset_id)
        graph_id = db.get(GraphBuild, ds.build_id).graph_id
        for q in db.execute(select(Question).where(Question.dataset_id == dataset_id)).scalars():
            if q.family in ("history", "false_premise") or q.gold_cypher.startswith("//"):
                continue  # gold is derived from rows (history/premise) or from outside data (SWE-bench)
            params = {**q.gold_params, "build_id": graph_id}
            current = sorted({r["gold"] for r in store.read(q.gold_cypher, params)})
            checked += 1
            if current != sorted(q.gold_answer):
                drift.append((q.id, q.family, sorted(set(current) ^ set(q.gold_answer))[:5]))
    console.print(f"checked {checked} questions, {len(drift)} drifted")
    for qid, fam, diff in drift:
        console.print(f"  question {qid} ({fam}): {diff}")
    raise typer.Exit(1 if drift else 0)


def _run_experiments(dataset_id: int, models: list[str], pipelines: list[str], prompt_version: str,
                     judge_model: str | None, options: dict) -> list[int]:
    from app.pipelines import PIPELINES

    unknown = set(pipelines) - PIPELINES.keys()
    if unknown:
        console.print(f"[red]unknown pipelines {sorted(unknown)}; choose from {sorted(PIPELINES)}[/red]")
        raise typer.Exit(2)
    ids = []
    with session_scope() as db:
        ds = db.get(Dataset, dataset_id)
        if ds is None or ds.status != "done":
            console.print(f"[red]dataset {dataset_id} not found or not finished[/red]")
            raise typer.Exit(2)
        build = db.get(GraphBuild, ds.build_id)
        if "vector" in pipelines and not (build.stats or {}).get("embeddings", {}).get("embedding_model"):
            console.print("[red]the vector pipeline needs a build with code embeddings[/red]")
            raise typer.Exit(2)
        for model in models:
            for pipeline in pipelines:
                exp = Experiment(dataset_id=dataset_id, model=model, pipeline=pipeline,
                                 prompt_version=prompt_version, judge_model=judge_model,
                                 options={k: v for k, v in options.items() if v is not None})
                db.add(exp)
                db.flush()
                ids.append(exp.id)
    for exp_id in ids:
        with session_scope() as db:
            e = db.get(Experiment, exp_id)
            console.print(f"running experiment {exp_id}: {e.model} x {e.pipeline}")
        run_experiment(exp_id)
        for _ in range(FAILED_CALL_RETRIES):  # outlast rate limits longer than the client's own backoff
            with session_scope() as db:
                failed = db.scalar(select(func.count()).select_from(Answer).where(
                    Answer.experiment_id == exp_id, Answer.error.is_not(None)))
            if not failed:
                break
            console.print(f"  {failed} failed model call(s); asking them again")
            run_experiment(exp_id)
        _check(Experiment, exp_id)
    return ids


@app.command()
def run(dataset_id: int, models: str = typer.Option(..., help="comma-separated LiteLLM model names"),
        pipelines: str = "none,vector,graphrag,cypher_agent", prompt_version: str = "v1",
        judge_model: str = typer.Option(None), limit: int = typer.Option(None), families: str = typer.Option(None),
        concurrency: int = 4, run_tests: bool = False, top_k: int = 8, graphrag_hops: int = 2) -> None:
    """Steps 5-6: answer every question with each model x pipeline and score the answers."""
    _setup()
    _run_experiments(dataset_id, _csv(models), _csv(pipelines), prompt_version, judge_model,
                     {"limit": limit, "families": _csv(families), "concurrency": concurrency, "run_tests": run_tests,
                      "top_k": top_k, "graphrag_hops": graphrag_hops})
    report(dataset_id, as_json=False)


@app.command()
def resume(experiment_id: int) -> None:
    """Finish a failed or interrupted experiment, or re-ask its failed model calls (answered questions are kept)."""
    _setup()
    with session_scope() as db:
        exp = db.get(Experiment, experiment_id)
        exp.status, dataset_id = "queued", exp.dataset_id
    run_experiment(experiment_id)
    _check(Experiment, experiment_id)
    report(dataset_id, as_json=False)


def _fmt(value) -> str:
    return "-" if value is None else (f"{value:.3f}" if isinstance(value, float) else str(value))


@app.command()
def report(dataset_id: int, as_json: bool = typer.Option(False, "--json")) -> None:
    """Leaderboard (models x pipelines), F1 per family and by hop depth."""
    _setup()
    with session_scope() as db:
        board, hops = leaderboard(db, dataset_id), hop_curve(db, dataset_id)
    if as_json:
        console.print_json(json.dumps({"leaderboard": board, "hops": hops}, default=str))
        return
    families = sorted({f for row in board for f in (row.get("f1_by_family") or {})})
    table = Table(title=f"Leaderboard - dataset {dataset_id}")
    for col in ["exp", "model", "pipeline", "F1 [95% CI]", *families, "ctx recall", "contradicted", "$/q", "ms/q",
                "failed calls"]:
        table.add_column(col)
    for row in board:
        ci = row.get("f1_ci") or [None, None]
        f1 = f"{_fmt(row.get('f1'))} [{_fmt(ci[0])}, {_fmt(ci[1])}]"
        errors = row.get("errors") or 0
        failed = f"{errors} (F1 answered {_fmt(row.get('f1_answered'))})" if errors else "0"
        table.add_row(str(row["experiment_id"]), row["model"], row["pipeline"], f1,
                      *[_fmt((row.get("f1_by_family") or {}).get(f)) for f in families],
                      _fmt(row.get("context_recall")), _fmt(row.get("contradicted_claim_rate")),
                      _fmt(row.get("cost_per_question")), _fmt(row.get("latency_ms_mean")), failed)
    console.print(table)
    if hops:
        hop_table = Table(title="F1 by hop depth (impact questions, E2)")
        for col in ("model", "pipeline", "hop", "F1"):
            hop_table.add_column(col)
        for p in sorted(hops, key=lambda p: (p["model"], p["pipeline"], p["hop"])):
            hop_table.add_row(p["model"], p["pipeline"], str(p["hop"]), _fmt(p["f1"]))
        console.print(hop_table)


@app.command("compare")
def compare_cmd(a: int, b: int, metric: str = "f1") -> None:
    """Paired bootstrap difference between two experiments on the same questions."""
    _setup()
    with session_scope() as db:
        console.print_json(json.dumps(compare(db, a, b, metric)))


@app.command("audit-edges")
def audit_edges(build_id: int, out: Path = Path("edge_audit.csv"), per_resolver: int = 100, seed: int = 0) -> None:
    """Sample CALLS edges per resolver into a CSV; fill the `label` column with correct/incorrect."""
    _setup()
    rows = sample_edges(get_store(), _graph_id(build_id), per_resolver, seed)
    with out.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["caller", "callee", "resolved_by", "file_path", "line", "label", "notes"])
        writer.writeheader()
        for r in rows:
            writer.writerow({**r, "label": "", "notes": ""})
    console.print(f"wrote {len(rows)} edges to {out}")


@app.command("audit-score")
def audit_score(path: Path) -> None:
    """Resolver precision from a hand-labeled edge audit CSV."""
    with path.open() as fh:
        rows = list(csv.DictReader(fh))
    console.print_json(json.dumps(edge_precision(rows)))


@app.command("audit-semantic")
def audit_semantic(build_id: int, judge_model: str, n: int = 50, seed: int = 0,
                   out: Path = Path("semantic_audit.csv")) -> None:
    """Grade a sample of LLM summaries and feature tags with a judge model; export a CSV for human grading."""
    _setup()
    from app.eval.judge import judge_semantic
    from app.llm.client import get_llm

    store, llm = get_store(), get_llm()
    rows = store.read(
        "MATCH (f:Function {build_id: $b}) WHERE f.summary IS NOT NULL OPTIONAL MATCH (f)-[i:IMPLEMENTS]->(t:Feature) "
        "RETURN f.qualified_name AS qn, f.code AS code, f.summary AS summary, collect(t.name) AS features "
        "ORDER BY qn", {"b": _graph_id(build_id)})
    rng = random.Random(seed)
    sample = rng.sample(rows, min(n, len(rows)))
    with session_scope() as db:
        repo = db.get(GraphBuild, build_id).repo
        records = []
        for r in sample:
            items = [("summary", r["summary"])] + [("feature", f) for f in r["features"]]
            for kind, content in items:
                label = judge_semantic(llm, judge_model, repo, r["code"] or "", "summary" if kind == "summary"
                                       else "feature tag", content)
                db.add(SemanticAudit(build_id=build_id, function_qn=r["qn"], kind=kind, content=content,
                                     judge_model=judge_model, judge_label=label))
                records.append({"function": r["qn"], "kind": kind, "content": content, "judge_label": label,
                                "human_label": ""})
    with out.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["function", "kind", "content", "judge_label", "human_label"])
        writer.writeheader()
        writer.writerows(records)
    faithful = sum(r["judge_label"] == "faithful" for r in records)
    console.print(f"judged {len(records)} items, {faithful} faithful; CSV for human grading: {out}")


@app.command()
def swebench(build_id: int, instances: Path, version: str = typer.Option(None)) -> None:
    """Add SWE-bench localization questions (instances JSONL) whose base commit matches the build."""
    _setup()
    from app.testgen.swebench import swebench_drafts

    records = [json.loads(line) for line in instances.read_text().splitlines() if line.strip()]
    with session_scope() as db:
        b = db.get(GraphBuild, build_id)
        drafts, skipped = swebench_drafts(get_store(), b.graph_id, b.commit_sha, records)
        ds = Dataset(build_id=build_id, version=version or f"swebench-b{build_id}", status="done",
                     n_questions=len(drafts), options={"source": str(instances)})
        db.add(ds)
        db.flush()
        for d in drafts:
            db.add(Question(dataset_id=ds.id, dataset_version=ds.version, family=d.family, text=d.text,
                            template_text=d.text, gold_cypher=d.gold_cypher, gold_params=d.gold_params,
                            gold_answer=d.gold_answer, hop_count=None, target=d.target, meta=d.meta))
        console.print(f"dataset {ds.id}: {len(drafts)} questions, {len(skipped)} skipped")
    for s in skipped[:20]:
        console.print(f"  skipped {s}")


def _metric(summary: dict, path: str):
    value = summary
    for part in path.split("."):
        value = value.get(part) if isinstance(value, dict) else None
    return value


def check_thresholds(board: list[dict], thresholds: dict, baseline: dict | None) -> list[str]:
    failures = []
    max_drop = float(thresholds.get("max_regression", 0.05))
    for row in board:
        key = f"{row['model']}|{row['pipeline']}"
        rules = {**(thresholds.get("*") or {}), **(thresholds.get(row["pipeline"]) or {})}
        for metric, bounds in rules.items():
            value = _metric(row, metric)
            if value is None:  # a metric the thresholds require but the run did not produce
                failures.append(f"{key}: {metric} is missing")
                continue
            if "min" in bounds and value < bounds["min"]:
                failures.append(f"{key}: {metric} = {value:.3f} < min {bounds['min']}")
            if "max" in bounds and value > bounds["max"]:
                failures.append(f"{key}: {metric} = {value:.3f} > max {bounds['max']}")
        previous = (baseline or {}).get(key)
        if previous is not None and row.get("f1") is not None and row["f1"] < previous - max_drop:
            failures.append(f"{key}: f1 {row['f1']:.3f} dropped more than {max_drop} below baseline {previous:.3f}")
    return failures


@app.command()
def gate(config: Path = Path("../evals/gate.yaml"), update_baseline: bool = False) -> None:
    """CI regression gate: build the pinned repo, run the frozen slice, fail below the thresholds."""
    _setup(verbose=True)
    cfg = yaml.safe_load(config.read_text())
    root = config.parent
    build_opts = cfg.get("build", {})
    with session_scope() as db:
        repo = Repo(url=str((root / cfg["repo"]).resolve()) if cfg.get("repo_is_path") else cfg["repo"],
                    name=cfg.get("name", repo_name(cfg["repo"])), commit_sha=cfg.get("commit"))
        db.add(repo)
        db.flush()
        b = GraphBuild(repo_id=repo.id, repo=repo.name, parser_version=PARSER_VERSION,
                       options={"commit_sha": cfg.get("commit"), **build_opts})
        db.add(b)
        db.flush()
        build_id = b.id
    run_build(build_id)
    _check(GraphBuild, build_id)
    dataset_id = load_dataset_file(build_id, root / cfg["dataset"], cfg.get("name"))
    _run_experiments(dataset_id, cfg["models"], cfg["pipelines"], cfg.get("prompt_version", "v1"),
                     cfg.get("judge_model"), {"concurrency": cfg.get("concurrency", 4), **cfg.get("options", {})})
    report(dataset_id, as_json=False)
    with session_scope() as db:
        board = leaderboard(db, dataset_id)
    thresholds = yaml.safe_load((root / cfg["thresholds"]).read_text()) if cfg.get("thresholds") else {}
    baseline_path = root / cfg.get("baseline", "baseline.json")
    baseline = json.loads(baseline_path.read_text()) if baseline_path.exists() else None
    results = {f"{r['model']}|{r['pipeline']}": r.get("f1") for r in board}
    out_dir = root / "results"
    out_dir.mkdir(exist_ok=True)
    (out_dir / f"{cfg.get('name', 'gate')}.json").write_text(json.dumps(board, indent=2, default=str))
    if update_baseline:
        baseline_path.write_text(json.dumps(results, indent=2, sort_keys=True) + "\n")
        console.print(f"baseline written to {baseline_path}")
        return
    failures = check_thresholds(board, thresholds, baseline)
    if failures:
        console.print("[red]eval gate FAILED[/red]")
        for f in failures:
            console.print(f"  - {f}")
        raise typer.Exit(1)
    console.print("[green]eval gate passed[/green]")


def _finished_build(build_id: int) -> GraphBuild:
    with session_scope() as db:
        b = db.get(GraphBuild, build_id)
        if b is None or b.status != "done":
            console.print(f"[red]build {build_id} not found or not finished[/red]")
            raise typer.Exit(2)
        b.repository  # noqa: B018 - load the relationship before the session closes
        return b


@app.command()
def ask(build_id: int, question: str, model: str = typer.Option(..., help="LiteLLM model name"),
        pipeline: str = "graphrag", judge_model: str = typer.Option(None), graphrag_hops: int = 2,
        as_json: bool = typer.Option(False, "--json")) -> None:
    """Ask a free-form question about a build; every claim in the answer is checked against the graph."""
    _setup()
    from app.analysis.ask import BuildRef
    from app.analysis.ask import ask as ask_graph
    from app.eval.index import load_build_index
    from app.llm.client import get_llm

    b = _finished_build(build_id)
    store = get_store()
    ref = BuildRef(b.id, b.graph_id, b.repository.name, b.commit_sha,
                   (b.stats or {}).get("embeddings", {}).get("embedding_model"))
    result = ask_graph(store, get_llm(), load_build_index(store, b.graph_id), ref, question, model, pipeline,
                       judge_model=judge_model, options={"graphrag_hops": graphrag_hops})
    if as_json:
        console.print_json(json.dumps(result, default=str))
        return
    if result["error"]:
        console.print(f"[red]{result['error']}[/red]")
        raise typer.Exit(1)
    for item in result["answer"]:
        mark = {True: "[green]in graph[/green]", False: "[red]not in the repo[/red]", None: ""}[item["in_graph"]]
        console.print(f"  - {escape(item['name'])} {mark}")
    console.print(result["explanation"] or "", markup=False)
    for c in result["claims"]:  # model text may contain [brackets]: print it literally
        console.print(f"  ({c['verdict']}) {c['subject']} {c['relation']} {c['object']}: {c['evidence']}",
                      markup=False, highlight=False)


@app.command()
def impact(build_id: int, diff: Path = typer.Option(None, help="unified diff file (old side = the build's commit)"),
           function: list[str] = typer.Option(None, "--function", "-f", help="a changed function (repeatable)"),
           hops: int = 3, as_json: bool = typer.Option(False, "--json"),
           markdown: Path = typer.Option(None, help="also write a Markdown summary here (e.g. for a PR comment)")) -> None:
    """Functions and tests a change can affect, from the precise call graph (no LLM)."""
    _setup()
    from app.analysis.impact import analyze_impact
    from app.eval.index import load_build_index

    if diff is None and not function:
        console.print("[red]give --diff and/or --function[/red]")
        raise typer.Exit(2)
    b = _finished_build(build_id)
    report = analyze_impact(load_build_index(get_store(), b.graph_id), diff.read_text() if diff else None,
                            list(function or []), hops)
    text = report.to_markdown(f"VeriGraph change impact ({b.repo} @ {(b.commit_sha or 'working tree')[:10]})",
                              b.unresolved_call_rate)
    if markdown:
        markdown.write_text(text)
    if as_json:
        console.print_json(json.dumps(report.to_dict()))
    else:
        console.print(text, markup=False, highlight=False)


@app.command("audit-symbols")
def audit_symbols(build_id: int) -> None:
    """Run the symbol checker on the repository's own code: anything it flags is a checker false positive."""
    _setup()
    from app.analysis.symbol_audit import symbol_self_audit
    from app.eval.index import load_build_index

    store, graph_id = get_store(), _graph_id(build_id)
    console.print_json(json.dumps(symbol_self_audit(store, graph_id, load_build_index(store, graph_id))))


@app.command("delete-build")
def delete_build_cmd(build_id: int, yes: bool = typer.Option(False, "--yes", help="do not ask for confirmation")) -> None:
    """Delete a build's graph (Neo4j), checkout (disk) and results (datasets, experiments, answers)."""
    _setup()
    from app.cleanup import active_jobs, delete_build
    from app.config import get_settings

    with session_scope() as db:
        if db.get(GraphBuild, build_id) is None:
            console.print(f"[red]build {build_id} not found[/red]")
            raise typer.Exit(2)
        running = active_jobs(db, build_id)
        if running:
            console.print(f"[red]wait for these jobs to finish first: {', '.join(running)}[/red]")
            raise typer.Exit(1)
        if not yes and not typer.confirm(f"Delete build {build_id} and all its datasets and results?"):
            raise typer.Exit(1)
        console.print_json(json.dumps(delete_build(db, get_store(), build_id, get_settings().data_dir)))


def main() -> None:  # pragma: no cover
    app()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(app())
