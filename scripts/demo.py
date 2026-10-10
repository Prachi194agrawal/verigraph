"""Drive the real API and worker from a repository to a leaderboard, using returned IDs."""

from __future__ import annotations

import argparse
import json
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def request(base: str, path: str, body: dict | None = None):
    payload = json.dumps(body).encode() if body is not None else None
    req = Request(base.rstrip("/") + path, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=30) as response:
            return json.load(response)
    except HTTPError as exc:
        raise RuntimeError(f"{path}: HTTP {exc.code}: {exc.read().decode()}") from exc
    except URLError as exc:
        raise RuntimeError(f"Cannot reach {base}: {exc.reason}. Start Docker Compose first.") from exc


def wait(base: str, path: str, timeout: int) -> dict:
    deadline = time.monotonic() + timeout
    previous = None
    while time.monotonic() < deadline:
        row = request(base, path)
        message = (row["status"], row.get("progress"))
        if message != previous:
            print(f"  {path}: {message[0]} — {message[1] or ''}", flush=True)
            previous = message
        if row["status"] == "done":
            return row
        if row["status"] == "failed":
            raise RuntimeError(f"{path} failed:\n{row.get('error')}")
        time.sleep(2)
    raise RuntimeError(f"Timed out waiting for {path}; the job may still be running. Check worker logs.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument("--web", default="http://localhost:3000")
    parser.add_argument("--repo", default="/fixtures/mini_repo", help="URL or path accessible to the worker")
    parser.add_argument("--commit", default=None)
    parser.add_argument("--models", default="mock/extractive,mock/empty")
    parser.add_argument("--embedding-model", default="hash")
    parser.add_argument("--semantic-model", default=None)
    parser.add_argument("--history", action="store_true")
    parser.add_argument("--github", action="store_true")
    parser.add_argument("--per-family", type=int, default=5)
    parser.add_argument("--limit", type=int, default=None, help="answer at most this many questions per experiment")
    parser.add_argument("--pipelines", default="none,vector,graphrag,cypher_agent")
    parser.add_argument("--timeout", type=int, default=1800, help="seconds per job")
    args = parser.parse_args()
    models = list(dict.fromkeys(m.strip() for m in args.models.split(",") if m.strip()))
    pipelines = list(dict.fromkeys(p.strip() for p in args.pipelines.split(",") if p.strip()))
    if not models or not pipelines or args.per_family < 1 or args.timeout < 1 or (args.limit is not None and args.limit < 1):
        parser.error("models and pipelines must not be empty; per-family, limit and timeout must be positive")
    try:
        health = request(args.api, "/health")
        if health.get("database") != "ok" or health.get("neo4j") != "ok":
            raise RuntimeError(f"Services are not ready: {health}")
        print("Step 1: register repository", flush=True)
        repo = request(args.api, "/repos", {"url": args.repo, "commit_sha": args.commit})
        print("Steps 2–3: structure, optional history/meaning, embeddings", flush=True)
        build = request(args.api, f"/repos/{repo['id']}/builds", {
            "history": args.history, "github": args.github, "max_commits": 100,
            "embedding_model": args.embedding_model, "semantic_model": args.semantic_model,
            "semantic_limit": 20,
        })
        wait(args.api, f"/builds/{build['id']}", args.timeout)
        print("Step 4: generate exact-answer questions", flush=True)
        dataset = request(args.api, "/datasets", {"build_id": build["id"], "per_family": args.per_family})
        dataset = wait(args.api, f"/datasets/{dataset['id']}", args.timeout)
        if not dataset["n_questions"]:
            raise RuntimeError("No eligible questions were generated; use a repository with functions and tests.")
        print(f"Steps 5–6: answer and score {len(models)} model(s) × {len(pipelines)} pipeline(s)", flush=True)
        experiments = request(args.api, "/experiments", {
            "dataset_id": dataset["id"], "models": models, "pipelines": pipelines, "concurrency": 2,
            "limit": args.limit,
        })
        for experiment in experiments:
            wait(args.api, f"/experiments/{experiment['id']}", args.timeout)
            results = request(args.api, f"/experiments/{experiment['id']}/results")
            errors = [row for row in results["answers"] if row.get("error")]
            if errors:
                raise RuntimeError(f"Experiment {experiment['id']} has {len(errors)} answer errors: {errors[0]['error']}")
        print("Step 7: leaderboard", flush=True)
        board = request(args.api, f"/leaderboard?dataset_id={dataset['id']}")
        print(f"{'Model':32} {'Pipeline':14} {'F1':>7} {'Answers':>7} {'Errors':>6}")
        for row in board:
            f1 = f"{row['f1']:.3f}" if row.get("f1") is not None else "—"
            print(f"{row['model']:32} {row['pipeline']:14} {f1:>7} {row['n']:>7} {row['errors']:>6}")
        print(f"Dashboard: {args.web.rstrip('/')}/datasets/{dataset['id']}/leaderboard")
        print("Step 8: run the CI smoke gate using the commands in README.md.")
        print("Mock models and hash embeddings verify plumbing; they are not research benchmark results.")
        return 0
    except (RuntimeError, TimeoutError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
