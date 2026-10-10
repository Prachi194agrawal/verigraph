# VeriGraph for Code: architecture

VeriGraph for Code turns a Python repository into a **code knowledge graph** and then uses that graph to
**test and grade LLMs** on the same codebase. A parser and static resolvers build the structure, not an
LLM, so the graph can act as ground truth: questions get exact gold answers and claims get exact checks.

This document follows one run from a repository URL to a graded leaderboard, step by step. For every
step it says what triggers it, which code runs, what goes in and comes out, how the algorithm works, and
what its limits are. Section 5 then traces one real question from psf/requests through the whole system.

## Start here: follow the working system

Use the [first-setup commands in README.md](README.md#first-setup-no-api-keys-needed), then run
`python3 scripts/demo.py`. This drives the HTTP API and Celery worker, not an in-memory simulation.
The default fixture is mounted read-only at `/fixtures/mini_repo` in both backend containers. The
worker copies it into its own build checkout before parsing. New IDs come from PostgreSQL on every run.

| Stage | Action you can observe | Where it runs | Output / how to inspect it |
|---|---|---|---|
| 1. Register and ingest | demo calls `POST /repos`, then starts a build | API registers; worker checks out/copies | `GET /builds/{id}` reports progress; a remote pin becomes a SHA |
| 2. Structure | parse Python files and resolve calls | `graph/builder.py`, parser, symbols, resolvers | Neo4j nodes/edges; build quality shows resolver coverage |
| 3. History, meaning and vectors | history/meaning are opt-in in the demo; hash vectors always enabled | ingest and semantic modules in worker | build stats and function properties in Neo4j |
| 4. Questions | demo calls `POST /datasets` and waits | Cypher templates in worker | question text, gold query and exact answer in PostgreSQL |
| 5. Answers | demo calls `POST /experiments` for each model × pipeline | worker threads, retrieval, LiteLLM | one answer per question; retrieved context, tokens, cost and latency |
| 6. Verdicts | scorer checks each completed answer | `eval/scorer.py` and checker modules | scores, graph evidence and symbol violations in PostgreSQL |
| 7. Explore | demo prints a URL with the actual dataset ID | browser → API | leaderboard, family/hop charts, graph explorer and per-answer checks |
| 8. Regression gate | run `make gate-smoke`; CI runs it on pushes/PRs | CLI or GitHub Actions | fixed fixture questions checked against thresholds |
| Use the graph directly | "Ask the graph" and "Change impact" on a build page, or `verigraph ask` / `verigraph impact` | API process (synchronous) | answer with every claim checked; impacted callers and the tests to run ([section 4b](#4b-using-the-graph-directly-ask-and-change-impact)) |

The fixture has no git history, and mock models don't provide meaningful summaries or measured model
ability. To exercise history and real semantic enrichment, use the real-repository command in README.
The architecture below separates implemented mechanisms from research measurements that require real
models, external benchmark data and human labels.

### API keys and data flow

The root `.env` feeds API/worker containers through Compose. LiteLLM reads `GROQ_API_KEY`,
`GEMINI_API_KEY`, `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` for the selected model provider. The
browser receives only the public API address. One provider key is sufficient for all optional LLM
stages when they use that provider. `GITHUB_TOKEN` serves GitHub history enrichment, not LLM calls.
`LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` are optional tracing credentials. Mock models, local
Ollama and hash embeddings need no provider API key. See README for provider links and run commands.

`INSTALL_EMBEDDINGS=false` selects a smaller backend Docker image; pair it with `EMBEDDING_MODEL=hash`.
The build argument applies to both API and worker. Neural embedding models need the embeddings extra
and model downloads. Hash and CodeRankEmbed both use 768 dimensions by default, but their vectors
represent different spaces, so interpret comparisons accordingly.

**Local paths are confined.** The API has no authentication and serves files from build checkouts, so a
local-path repository must live under `LOCAL_REPO_ROOTS` (Compose default: `/fixtures,/repos`). Git URLs
are always accepted. An empty `LOCAL_REPO_ROOTS` allows any local path and is meant only for single-user
local development. Do not expose the API on a public network without putting authentication in front of it.

### Job validation and recovery

Requests reject empty repository/model names, unknown or empty resolver lists, nonpositive limits,
empty model/pipeline lists, and concurrency outside 1–32. Model/pipeline duplicates are removed before
creating work. The vector pipeline is rejected before enqueueing when its graph has no embeddings.
Human claim labels must reference a verdict belonging to that same answer, preserving agreement data.
Only failed experiments, or finished ones with failed model calls, can be resumed through the HTTP API;
queued/running jobs may still have an active worker and must not be duplicated. A worker interrupted without recording failure requires
operator investigation before recovery. The CLI resume command should likewise only be used after
confirming no worker is still processing that experiment.

**Contents**

1. [The system in one picture](#1-the-system-in-one-picture)
2. [Components and where they run](#2-components-and-where-they-run)
3. [Repository layout](#3-repository-layout)
4. [A run, step by step](#4-a-run-step-by-step)
   - [Step 1: Ingest](#step-1-ingest)
   - [Step 2: Build the structure layer](#step-2-build-the-structure-layer)
   - [Step 3: Add history and meaning](#step-3-add-history-and-meaning)
   - [Step 4: Generate the test set](#step-4-generate-the-test-set)
   - [Step 5: Run experiments](#step-5-run-experiments)
   - [Step 6: Score](#step-6-score)
   - [Step 7: Explore](#step-7-explore)
   - [Step 8: Gate regressions](#step-8-gate-regressions)
   - [4b. Using the graph directly: Ask and Change impact](#4b-using-the-graph-directly-ask-and-change-impact)
5. [Follow one question through the system](#5-follow-one-question-through-the-system)
6. [Data model](#6-data-model)
7. [Trusting the graph and the scores](#7-trusting-the-graph-and-the-scores)
8. [Running the six experiments](#8-running-the-six-experiments)
9. [Interfaces: HTTP API and CLI](#9-interfaces-http-api-and-cli)
10. [Configuration](#10-configuration)
11. [Running it](#11-running-it)
12. [Tests and CI](#12-tests-and-ci)
13. [Design decisions and deviations from the plan](#13-design-decisions-and-deviations-from-the-plan)
14. [Known limitations](#14-known-limitations)
15. [Extending the system](#15-extending-the-system)
16. [Review log: what was corrected and verified](#16-review-log-what-was-corrected-and-verified)
17. [Roadmap: what to add next](#17-roadmap-what-to-add-next)

---

## 1. The system in one picture

```mermaid
flowchart LR
  subgraph build["Steps 1-3: once per pinned commit"]
    url["repo URL + commit"] --> ingest["1. Ingest<br/>mirror + checkout"]
    ingest --> structure["2. Structure<br/>Tree-sitter + resolvers"]
    structure --> history["3a. History<br/>PyDriller + GitHub"]
    history --> neo[("Neo4j<br/>code graph")]
    neo --> meaning["3b. Meaning<br/>summaries, features, embeddings"]
    meaning --> neo
  end
  subgraph evaluate["Steps 4-8: rerun on every model, prompt or pipeline change"]
    neo --> testgen["4. Test set<br/>Cypher templates = exact gold"]
    testgen --> pg[("PostgreSQL<br/>questions, answers, scores")]
    pg --> run["5. Experiments<br/>models x pipelines via LiteLLM"]
    neo --> run
    run --> score["6. Scoring<br/>set-F1, claim checks, symbol checks"]
    neo --> score
    score --> pg
    pg --> ui["7. Dashboard"]
    pg --> gate["8. CI eval gate"]
  end
```

The **three graph layers** come from different sources and carry different trust:

| Layer | Source | Becomes gold? |
|---|---|---|
| Structure: files, modules, classes, functions, imports, calls, inheritance, tests | Tree-sitter + SCIP/Jedi/import resolution | Yes, when resolved precisely (`resolved_by` = scip, jedi or import) |
| History: commits, authors, PRs, issues, the functions each commit changed | git (PyDriller) + GitHub API | Yes |
| Meaning: one-line summaries, feature tags, embeddings | an LLM / an embedding model | Never. Feature edges carry `confidence < 1` |

The **two stores** have separate jobs. Neo4j holds facts about the repository. PostgreSQL (SQLite when
running locally) holds everything about *evaluating* models on it.

## 2. Components and where they run

```mermaid
flowchart TB
  browser["Browser<br/>Next.js dashboard :3000"] -->|REST| api["FastAPI :8000<br/>backend/app/main.py"]
  cli["verigraph CLI<br/>backend/app/cli.py"] --> jobs
  api -->|enqueue| queue{{"JOB_BACKEND"}}
  queue -->|celery| redis[("Redis")] --> worker["Celery worker<br/>workers/celery_app.py"]
  queue -->|inline| thread["thread in the API process"]
  worker --> jobs["Job bodies<br/>workers/jobs.py"]
  thread --> jobs
  jobs --> neo[("Neo4j 5.x<br/>graph + vector index")]
  jobs --> pg[("PostgreSQL / SQLite<br/>results")]
  jobs --> litellm["LiteLLM gateway<br/>llm/client.py"]
  litellm --> providers["Groq · Gemini · OpenAI · Anthropic · Ollama"]
  litellm -.-> langfuse["Langfuse (optional)"]
  jobs -.-> sandbox["Docker sandbox (optional)<br/>eval/sandbox.py"]
  api --> neo
  api --> pg
```

| Component | Technology | Code | Notes |
|---|---|---|---|
| API | FastAPI, Pydantic v2 | [backend/app/main.py](backend/app/main.py), [backend/app/api/](backend/app/api/) | Creates tables at startup and applies the Neo4j schema |
| Jobs | Celery + Redis, or a thread | [backend/app/workers/](backend/app/workers/) | `JOB_BACKEND=celery` in Docker, `inline` locally. Each forked Celery child opens its own database and Neo4j connections, because sockets inherited from the parent would be shared and corrupted |
| CLI | Typer | [backend/app/cli.py](backend/app/cli.py) | Runs every step synchronously; the CI gate uses it |
| Graph store | Neo4j 5.26 LTS + APOC + vector index (only features available since 5.15 are used) | [backend/app/graph/store.py](backend/app/graph/store.py), [schema.cypher](backend/app/graph/schema.cypher) | One database; builds are separated by their `build_id` property, which holds the build's random graph key (`graph_builds.graph_id`) |
| Results DB | PostgreSQL 16 (SQLite locally) via SQLAlchemy 2 | [backend/app/models.py](backend/app/models.py), [backend/app/db.py](backend/app/db.py) | Tables are created with `create_all`, under a Postgres advisory lock because the API and the worker both start at once |
| LLM gateway | LiteLLM | [backend/app/llm/client.py](backend/app/llm/client.py) | Disk cache, cost, tracing, offline mock models |
| Tracing | JSONL files, plus Langfuse v3 (self-hosted) | [backend/app/llm/tracing.py](backend/app/llm/tracing.py) | Langfuse runs under the compose profile `tracing` |
| Dashboard | Next.js 16, Cytoscape.js, Monaco, Recharts | [frontend/](frontend/) | Fetches from the API in the browser |
| Sandbox | Docker | [backend/app/eval/sandbox.py](backend/app/eval/sandbox.py) | Stretch goal; off by default |

## 3. Repository layout

```
verigraph-code/
├── architecture.md            this document
├── README.md                  quick start
├── docker-compose.yml         neo4j, postgres, redis, api, worker, web (+ profiles: tracing, ollama)
├── .env.example               API keys and settings
├── Makefile                   up / test / api / web / gate-smoke / gate
├── docker/postgres-init.sql   creates the langfuse database
├── backend/
│   ├── pyproject.toml         package "verigraph-code", CLI entry point "verigraph"
│   ├── Dockerfile
│   ├── app/
│   │   ├── __init__.py        version, PARSER_VERSION
│   │   ├── config.py          Settings (env vars), PRECISE_RESOLVERS
│   │   ├── db.py, models.py   results database (SQLAlchemy)
│   │   ├── schemas.py         API request/response bodies
│   │   ├── main.py, cli.py    FastAPI app, Typer CLI
│   │   ├── api/               routers: repos, builds (+ ask, impact, delete), datasets, experiments, results
│   │   ├── analysis/          ask.py (free-form questions), impact.py (change impact), symbol_audit.py
│   │   ├── cleanup.py         delete a build from Neo4j, disk and the results database
│   │   ├── ingest/            clone.py (checkout), history.py (PyDriller), github.py (PyGithub)
│   │   ├── graph/             modules.py, parser.py, symbols.py, resolver.py, scip.py,
│   │   │                      builder.py, model.py, store.py, schema.cypher
│   │   ├── semantic/          embeddings.py, summaries.py, features.py
│   │   ├── testgen/           templates.py (families), generator.py, filters.py,
│   │   │                      rephraser.py, swebench.py
│   │   ├── pipelines/         base.py, no_retrieval.py, vector.py, graphrag.py,
│   │   │                      cypher_agent.py, parsing.py, context.py
│   │   ├── eval/              index.py, normalize.py, set_f1.py, claim_check.py,
│   │   │                      symbol_check.py, judge.py, sandbox.py, meta_eval.py,
│   │   │                      scorer.py, report.py
│   │   ├── llm/               client.py (LiteLLM), cache.py, mock.py, tracing.py,
│   │   │                      prompts/*.v1.txt
│   │   └── workers/           jobs.py (job bodies), celery_app.py, dispatch.py
│   └── tests/                 pytest suite + fixtures/mini_repo (a repo with a hand-known graph)
├── frontend/                  Next.js dashboard (app/, components/, lib/)
├── evals/                     gate configs, thresholds, baselines, frozen datasets/*.jsonl
└── .github/workflows/eval.yml tests, smoke gate, frontend build, real eval gate
```

---

## 4. A run, step by step

Steps 1-3 run once per pinned commit as one **build job**. Step 4 is a **dataset job**. Steps 5-6 run as
one **experiment job** per model × pipeline pair. All three job types live in
[backend/app/workers/jobs.py](backend/app/workers/jobs.py). Each job writes `status` (queued, running,
done, failed), a throttled `progress` message, and on failure the traceback, onto its own database row.
That is how the dashboard follows along.

```mermaid
sequenceDiagram
  participant U as Dashboard / CLI
  participant A as API
  participant W as Worker
  participant N as Neo4j
  participant P as PostgreSQL
  participant L as LLM providers
  U->>A: POST /repos, POST /repos/{id}/builds
  A->>P: repos + graph_builds row (queued)
  A->>W: enqueue run_build(build_id)
  W->>W: steps 1-3 (checkout, parse, resolve, history)
  W->>N: write graph (build_id)
  W->>L: summaries / feature tags (optional)
  W->>N: embeddings + vector index
  W->>P: build done + stats
  U->>A: POST /datasets
  A->>W: run_dataset
  W->>N: seed + gold Cypher per question
  W->>P: questions
  U->>A: POST /experiments (models x pipelines)
  A->>W: run_experiment (one per pair)
  loop every question
    W->>N: retrieval (vector / subgraph / agent queries)
    W->>L: answer
    W->>W: score against the BuildIndex
    W->>P: answer, claim_verdicts, symbol_checks, scores
  end
  U->>A: GET /leaderboard, /answers/{id}/checks
```

### Step 1: Ingest

| | |
|---|---|
| Triggered by | `POST /repos` then `POST /repos/{id}/builds`, or `verigraph build URL --commit X` |
| Code | [ingest/clone.py](backend/app/ingest/clone.py) `checkout_repo` |
| Reads | a Git URL, a GitHub URL, a local git repo, or a local folder |
| Writes | `<DATA_DIR>/mirrors/<owner>__<name>.git`, `<DATA_DIR>/builds/<build_id>/repo`, `graph_builds.commit_sha` and `checkout_path` |

How it works:

- A **remote** repo is mirrored once (`git clone --mirror`), and later builds run `git remote update`
  on the mirror. Each build then gets its own clone of the mirror and checks out the pinned commit in
  detached HEAD. A pin can be a SHA, a tag (`v2.32.3`) or a branch. The resolved 40-character SHA is
  stored, so "the graph is pinned to one commit" holds even when you pinned a branch. A commit that only
  a PR ref can reach is fetched by SHA.
- **Builds of the same repository take turns** on its mirror (a file lock next to it), so a second build
  never updates a mirror that the first is still writing. A failed clone or update is retried twice, over
  HTTP/1.1, because GitHub can reset an HTTP/2 stream during a long transfer (`curl 92 ... early EOF`).
  The mirror includes every PR ref, so a popular repository downloads much more than its default branch.
- A **local git repo** is cloned too, so your working tree is never touched.
- A **plain folder** is copied and has no history.
- **Local paths** (folders, local repos, `file://` URLs) are accepted by the API only inside
  `LOCAL_REPO_ROOTS`, and a `../` cannot escape it because the path is resolved first. The check runs when a
  repo is registered and again when a build starts.
- The GitHub slug (`owner/name`) is parsed from the URL or from the `origin` remote. It enables the
  GitHub enrichment in step 3.

### Step 2: Build the structure layer

| | |
|---|---|
| Code | [graph/modules.py](backend/app/graph/modules.py) → [graph/parser.py](backend/app/graph/parser.py) → [graph/symbols.py](backend/app/graph/symbols.py) → [graph/resolver.py](backend/app/graph/resolver.py), orchestrated by [graph/builder.py](backend/app/graph/builder.py) `build_structure` |
| Reads | the checkout |
| Writes | an in-memory `CodeGraph` ([graph/model.py](backend/app/graph/model.py)), then Neo4j via `GraphStore.write_graph` |

The structure layer is built in memory first and written to Neo4j once. That keeps every algorithm
testable without a database: [tests/test_graph_build.py](backend/tests/test_graph_build.py) checks the
full graph of the fixture repo by hand.

**2a. Find files and name modules** (`modules.py`). Every `*.py` file is collected. Skipped: `.git`,
virtualenvs, `node_modules`, `build`, `dist`, hidden directories, and files over 1 MB. A file's module
name is its path relative to the first ancestor directory that is *not* a package (has no
`__init__.py`). That is how Python itself would import it: `src/requests/sessions.py` becomes
`requests.sessions`, and `tests/test_requests.py` becomes `test_requests` (or `tests.test_requests` if
`tests/` is a package). Colliding names, such as two scripts called `conf.py`, fall back to the full
dotted path. A file is a **test file** if it is named `test_*.py`, `*_test.py` or `conftest.py`, or if it
lives under a `tests/`, `test/` or `testing/` directory.

**2b. Parse each file** (`parser.py`). One Tree-sitter parse per file and one *iterative* walk. It uses
an explicit stack because deep expression trees can exceed Python's recursion limit. The walk tracks
scopes (module, class or function) and records:

| Recorded | Details |
|---|---|
| Functions | qualified name (`module.func`, `module.Class.method`, `module.outer.<locals>.inner`), signature text, parameters with kinds (positional-only, positional, `*args`, keyword-only, `**kwargs`) and defaults, decorators, docstring, line range (start includes decorators; `name_line` is the `def` line), dedented source, kind (function, method, staticmethod, classmethod, property), `is_async`, `is_nested` |
| Tests | `is_test` = test file + name `test*` + module level or inside a `Test*`/`*TestCase` class; plus a pytest node id such as `tests/test_core.py::TestEngine::test_turbo` |
| Classes | qualified name, base-class expressions as written (`adapters.BaseAdapter`, with `Generic[T]` reduced to `Generic` and `metaclass=` ignored), decorators, docstring, attributes (class-level assignments and `self.x = ...` in methods) |
| Imports | `import a.b as c`, `from x import y as z`, wildcards. Relative imports are resolved to absolute modules on the spot |
| Call sites | inside each function: the callee text (`self.send`), its last identifier (`send`), line and *character* column (Tree-sitter reports bytes; the resolvers need characters), positional count, keyword names, `*`/`**` splats, and `receiver_call` for chained calls (`Session().get` → `Session`, `super().run` → `super`) |
| Local bindings | parameters, assignments, `for`/`with`/`except` targets, walrus, `global`; `x = Foo(...)` assignments (for type inference); nested definitions; function-level imports |
| Returns | the callee of each `return Foo(...)`, plus `return self`, for return-type inference |

Calls in decorators and default values belong to the *enclosing* scope. Calls at module level or in class
bodies have no owning Function and are not recorded. A redefined name (conditional definitions, property
setters) is merged into one node; for `@overload` stubs the last, real implementation wins. Files with
syntax errors still yield every definition that parses, and the number of `ERROR` nodes is stored on
the File node.

**2c. Symbol table** (`symbols.py`). `SymbolTable.resolve_name(module, name)` answers "what does this name
mean at the top level of this module?". It checks top-level definitions first, then imports (the last
binding wins, with aliases), then `import *` targets, then module-level assignments, then builtins.
`resolve_import` follows re-exports: `from minipkg import run` reaches `minipkg/__init__.py`'s
`from .core import run` and lands on `minipkg.core.run`. It follows at most 8 hops. `member(target, attr)`
handles module attributes, submodules and class members. Class members use a depth-first, left-to-right
MRO over repository classes (`ClassHierarchy`).

**2d. Base classes → INHERITS.** Each base expression is resolved through the symbol table, giving
`resolved_by = import`. A base that cannot be resolved, but whose class name is unique in the repo,
becomes a `name` edge. Anything else (such as `Exception` or `dict`) is stored in the class's
`external_bases`.

**2e. Return-type inference.** `Function.returns` is set when every `return` produces the same repo
class: `return Engine(name)`, a classmethod's `return cls(...)`, or `return self`. Factories such as
`Engine.build` therefore count as returning `Engine`, which both the import resolver and the symbol
checker use.

**2f. IMPORTS edges.** File → Module for repository modules (the nearest module that has a file, for
namespace packages). File → ExternalPackage otherwise, flagged `is_stdlib` via
`sys.stdlib_module_names`.

**2g. Call resolution** (`resolver.py`). Each call site goes through the resolver chain
(`RESOLVERS=scip,jedi,import,name`). The first resolver that reaches a decision wins. A decision is
*resolved* (target functions in the repo), *external* (builtins, stdlib, third-party), or *no opinion*,
which passes the call site to the next resolver.

| Resolver | How it decides | Precise? |
|---|---|---|
| `scip` | Reads `index.scip` from `scip-python index` (run automatically if `scip-python` is on `PATH`, or uses an `index.scip` at the repo root). [graph/scip.py](backend/app/graph/scip.py) decodes the protobuf wire format directly, with no generated bindings. The reference occurrence at the call's position gives a symbol, and the symbol's definition occurrence gives the file and line → Function. | yes |
| `jedi` | `jedi.Script(...).goto(line, col, follow_imports=True)` at the callee's last identifier. The project's `sys.path` puts the repo's source roots first, so the repo's own package wins over an installed copy. A definition at (path, line) maps to the Function or Class with that `name_line`. Calling a class resolves to its `__init__` through the MRO. Results outside the repo count as external. | yes |
| `import` | The symbol table, plus: `self.m()` / `cls.m()` through the class MRO, `super().m()` from the next class in the MRO, `Class(...).m()`, enclosing-function scopes (closures, nested defs, local imports), and locals assigned exactly once from a call (`engine = Engine.build(...); engine.run()` uses 2e). A guard stops self-referential assignments such as `x = x.copy()`. | yes |
| `name` | Last resort: match the bare called name. Attribute calls match only methods, bare calls only top-level functions and class constructors. Names of built-in type methods (`get`, `items`, `append`...) are skipped, and more than 3 candidates is treated as unresolved. Edges record `ambiguity = number of candidates`. | **no**: stays in the graph for retrieval, never becomes gold |

Jedi resolves against the Python environment it runs in. Which third-party calls count as *external*
rather than *unresolved* can therefore shift slightly between machines (12.6 % locally against 12.7 % in
the Docker image for requests). Repo-internal edges, and so the gold answers, do not change.

Several call sites from the same caller to the same callee become **one** `CALLS` edge carrying every
line (`lines`), the first line (`line`), and the *best* resolver among them. The build also reports two
rates:

- `unresolved_call_rate` = (unresolved + dynamic) / (call sites − external calls)
- `name_only_rate` = name-matched call sites / (call sites − external calls)

The callee names left unresolved are stored on each Function (`unresolved_calls`), which the claim
checker uses later.

**2h. TESTS edges.** A test function gets a TESTS edge to every non-test-file function it calls directly
through a precise CALLS edge.

**2i. Namespaces.** For every module the resolved top-level bindings are stored as JSON on its File node
(`namespace_json`: `{"norm": ["function", "minipkg.utils.normalize"], ...}`). The eval-time symbol checker
needs this to know which names generated code may use without importing them.

**2j. Write to Neo4j** (`store.py`, `write_graph`).

- **Graph key**: the nodes of a build carry `build_id` = the build's `graph_id`, a random number between
  10^9 and 10^12 assigned when the build row is created. It is deliberately not the row id, so several
  results databases (the Docker stack's PostgreSQL, a local SQLite file, a CI run) can share one Neo4j
  without overwriting each other's graphs. API and CLI arguments stay row ids; the build page shows the
  key for queries in Neo4j Browser. Builds created before graph keys existed keep their row id as key
  (the startup migration backfills `graph_id = id`).
- **Guard**: if the key already holds a *different* repository, the write is refused rather than
  overwriting another repository's graph.
- **Replace**: the build's previous nodes are deleted per label (`CALL { ... } IN TRANSACTIONS`). Nodes
  and edges are then created with `UNWIND` batches of 2,000.
- **Keys**: every node gets `build_id` and `uid = "<build_id>:<kind>:<key>"`.

**Example: psf/requests at v2.32.3** (single-threaded parsing and resolution, no GitHub token; the whole build including 1,000 commits of history took
about 90 s):

| files | classes | functions (tests) | call sites | precise | name-only | external | unresolved rate | CALLS | INHERITS | TESTS |
|---|---|---|---|---|---|---|---|---|---|---|
| 36 | 86 | 667 (333) | 2,414 | 991 | 178 | 1,076 | 12.6 % | 1,009 | 36 | 554 |

### Step 3: Add history and meaning

**3a. History from git** ([ingest/history.py](backend/app/ingest/history.py), `mine_history`).
PyDriller walks the newest `MAX_HISTORY_COMMITS` (default 1,000) commits reachable from the pinned HEAD,
newest first.

- **Commit** and **Author** nodes. An author is keyed by lower-cased email; authored and committed times
  are stored as UTC ISO strings, so they sort as text. `order` = 0 for the newest commit.
- **MODIFIES** (Commit → Function). For each modified `.py` file that still exists at the pinned commit,
  the *same Tree-sitter parser* computes function line ranges on the after- and before-versions. A
  function is modified when added lines fall inside its after-range or deleted lines fall inside its
  before-range. This produces exactly the graph's qualified names; PyDriller's lizard-based
  `changed_methods` names methods differently and cannot tell same-named methods apart. Only functions
  that exist at the pinned commit are linked.
- **PullRequest** from commit messages, with no API needed:
  - a squash-merge title ending in `(#123)` links that commit to PR 123;
  - `Merge pull request #123 from ...` links the merge commit plus the branch commits
    (`git rev-list <merge>^1..<merge>^2`), **but only for merges on the first-parent chain of the pinned
    commit**. GitHub records a merge into the repository there. A merge title reached only through a second
    parent was made inside a contributor's fork, such as `Merge pull request #1 from kennethreitz/master`, which
    synced upstream into a fork. Its number is a PR of the fork, and its branch range would claim hundreds of
    unrelated upstream commits. On psf/requests this rule skips 19 such merges (`history.fork_merges_skipped`),
    and the number of commits claimed by more than one PR drops from 164 to 3;
  - `fixes/closes/resolves #N` in any commit of a PR becomes a FIXES reference.
- Merge commits carry no modifications, because PyDriller reports none; their branch commits do.

**3b. GitHub enrichment** ([ingest/github.py](backend/app/ingest/github.py), optional).

- **With `GITHUB_TOKEN`**: closed PRs are listed 100 per call and linked through `merge_commit_sha`, and
  commit-author emails are mapped to logins (`Author.login`).
- **For every PR**: its title and body are fetched, and its `fixes #N` references are parsed.
- **For every referenced issue**: its title, body, state and labels are fetched. Numbers that turn out to
  be PRs are dropped.
- **Without a token**: only 40 calls are spent (the anonymous limit is 60 per hour). About 60 % go to PRs
  and the rest to issue texts, because issue texts are what localization questions need.
- **Rate limits** end enrichment gracefully, and the build stats record `api_calls_capped`.

**3c. Summaries** ([semantic/summaries.py](backend/app/semantic/summaries.py), only when a `semantic_model`
is given). Functions outside test files are sent eight per call, as code and with ids. The model returns a JSON map
`{id: one-line summary}`, which is stored as `Function.summary` and `summary_model`.

**3d. Feature tags** ([semantic/features.py](backend/app/semantic/features.py)). The model first proposes
6-12 features from the README and per-module samples. Then, ten functions per call, it tags each function
with up to two of those features and a confidence. The result is Feature nodes and
`(:Function)-[:IMPLEMENTS {confidence ≤ 0.95, model, source: 'llm'}]->(:Feature)`. These are the only
LLM-made edges, and no gold query reads them.

**3e. Embeddings** ([semantic/embeddings.py](backend/app/semantic/embeddings.py)). `EMBEDDING_MODEL`
chooses the embedder:

- `nomic-ai/CodeRankEmbed`: the default; 768 dimensions; queries get its required prefix
  `Represent this query for searching relevant code:`.
- `jinaai/jina-code-embeddings-0.5b`: set `EMBEDDING_DIM=896`.
- `hash`: a dependency-free lexical embedder (signed feature hashing of identifier sub-tokens and token
  bigrams). It is used in tests and CI.

Each function's text (qualified name, signature, docstring and code, up to 4,000 characters, and at most
`EMBEDDING_MAX_TOKENS` = 512 tokens) becomes `Function.code_embedding`; summaries become
`summary_embedding`. A Neo4j **vector index**
(`function_code_embedding`, cosine) is created with the embedder's dimension. One Neo4j instance therefore
uses one dimension, and a mismatch raises a clear error.

Embedding cost and benefit, measured on requests: CodeRankEmbed on CPU took about 8 minutes for 667
functions on a 12-core machine; `hash` takes about a second. On the frozen 50-question slice, vector RAG's
context recall was 0.31 with CodeRankEmbed against 0.25 with `hash`, while GraphRAG reached 0.95. Most
structural questions are about relationships, which nearest-neighbour search over code text rarely
surfaces.

### Step 4: Generate the test set

| | |
|---|---|
| Triggered by | `POST /datasets {build_id, families, per_family, seed, rephrase_model}` or `verigraph testgen BUILD_ID` |
| Code | [testgen/templates.py](backend/app/testgen/templates.py) (the families), [generator.py](backend/app/testgen/generator.py), [filters.py](backend/app/testgen/filters.py), [rephraser.py](backend/app/testgen/rephraser.py) |
| Writes | `datasets` row + `questions` rows: `text`, `template_text`, `gold_cypher`, `gold_params`, `gold_answer`, `hop_count`, `target`, `meta` |

Every family is a class with a **seed query** (which targets it can ask about), a **gold query** (the exact
answer for one target), and a text template. The generator works in five stages:

1. It runs the seed query, which returns targets in a deterministic order.
2. It shuffles them with the dataset's `seed`.
3. For each target it runs the gold query, builds a draft question, and filters it.
4. It stops once a family reaches `per_family` questions.
5. It stores the Cypher and parameters with each question, so any gold answer can be re-derived later.

| Family | Example question (from requests) | Gold answer comes from |
|---|---|---|
| `lookup` | Which file defines the function `should_bypass_proxies`? | `(:File)-[:DEFINES]->(n {name})`, for names defined in exactly one non-test file |
| `callers` | Which non-test functions call `resolve_proxies` (defined in `src/requests/utils.py`) directly? | precise `CALLS`, 1 hop, callers not tests |
| `impact` | If `RequestsCookieJar.iteritems` changes, which test functions could fail? Count a test if it calls it directly or through one intermediate function. | precise `CALLS*1..k` from `is_test` functions, k = 1, 2, 3 |
| `inheritance` | Which classes inherit from `ConnectionError`, directly or indirectly? | precise `INHERITS*1..` |
| `history` | Who last changed `proxy_bypass_registry`, and in which pull request? | newest Commit that MODIFIES it → Author, and the PullRequest that INCLUDES it. If that commit sits in several PRs, only the author is asked |
| `localization` | (issue #6643, "Leading slash in uri followed by column fails") Which functions need to change to fix this issue? → `requests.adapters.HTTPAdapter.request_url` | functions outside test files MODIFIED by the commits of the PR that FIXES the issue |
| `false_premise` | Why does `Response.raise_for_status` call `Response.text`? | no `CALLS*1..3` path at all → gold `[]` (true controls: a direct precise call → gold `[a, b]`) |
| `codegen` | Implement `Session.merge_environment_settings` (signature, decorators such as `@staticmethod`, and docstring given) | the repo functions the original calls (precise), plus the tests that reach it for the sandbox |

The impact gold query, as stored with each question (`%d` is the hop limit):

```cypher
MATCH p = (:Function {build_id: $build_id, qualified_name: $qn})<-[:CALLS*1..%d]-(t:Function {is_test: true})
WHERE ALL(r IN relationships(p) WHERE r.resolved_by IN $precise)
RETURN t.qualified_name AS gold, min(length(p)) AS hops
```

The rules that make the gold trustworthy:

- **Precise edges only.** Every gold query filters on `resolved_by IN ['scip','jedi','import']`.
- **1-10 gold items.** List answers can then be scored without a judge. False premises are the exception
  (empty gold, or a 2-item chain for true controls).
- **Targets come from library code.** Functions in test files, dunder methods and properties are not
  asked about; a property is accessed, not called, so "does X call it" is ill-posed.
- **Impact needs exactly k hops.** A k-hop question is kept only if at least one test is *exactly* k hops
  away; otherwise it would duplicate the (k-1)-hop question. Hops 1/2/3 are balanced, which is what
  experiment E2 needs.
- **False premises are real.** `b` is never reachable from `a` within 3 hops through any resolver,
  including name edges, and `b`'s name does not even appear in `a`'s code or unresolved calls. Two of
  every three questions are false premises and one is a true control, so a model that always says "no"
  cannot win.
- **History skips ties.** Two commits in the same second make "last" ambiguous, so the question is
  dropped. A last commit that belongs to several PRs makes "which pull request" ambiguous, so only the
  author is asked. Author aliases (email, local part, GitHub login) are stored in `meta.aliases` for
  normalization.
- **Code tasks blank the target.** `meta.blank = <target>` tells retrieval never to show the function
  being rewritten (see step 5).

**Rephrasing** (optional, `rephrase_model`). An LLM may only reword a question. If any backticked span
does not survive verbatim, the template text is kept, so gold answers never change meaning. Issue texts
and code tasks are not rephrased.

**Freezing and verifying.**

- `verigraph freeze DATASET out.jsonl --n 50` writes a family-stratified, seeded slice (the CI gate's
  input).
- `verigraph load-dataset BUILD file.jsonl` imports a slice into another build.
- `verigraph verify DATASET` re-runs every gold query against the dataset's build and lists drifted
  questions.

When the requests slice was reloaded into a fresh rebuild of the same commit, it verified with **0 drift**.

**SWE-bench** (outside gold, experiment E5). `verigraph swebench BUILD instances.jsonl` reads SWE-bench
instances whose `base_commit` equals the build's commit, maps the gold patch's deleted and inserted lines
onto function ranges at that commit ([testgen/swebench.py](backend/app/testgen/swebench.py)), and creates
`localization` questions.

### Step 5: Run experiments

| | |
|---|---|
| Triggered by | `POST /experiments {dataset_id, models[], pipelines[], prompt_version, judge_model, limit, concurrency}` or `verigraph run DATASET --models ... --pipelines ...` |
| Code | [workers/jobs.py](backend/app/workers/jobs.py) `run_experiment`, [pipelines/](backend/app/pipelines/), [llm/client.py](backend/app/llm/client.py), [llm/prompts/](backend/app/llm/prompts/) |
| Writes | one `experiments` row per model × pipeline, then `answers` + checks + `scores` per question, and finally `experiments.summary` |

**The run loop.** Worker threads (`concurrency`, default 4) each take a question, call
`pipeline.answer(question, model)` and then `scorer.score(...)`, and hand the result back. **Only the main
thread writes to the database**, which keeps SQLite safe and Postgres sessions simple. A re-run skips
questions that already have an answer, so an interrupted experiment resumes. An answer whose model call
failed (rate limit, timeout, provider outage) is stored with its `error` and scored 0, but it is not
a wrong answer: a re-run deletes it and asks the question again. `verigraph run` and the demo script do
this automatically, and the dashboard offers **Retry failed calls**. A pipeline never sees the
gold answer: it receives a `QuestionInput` with only id, family, text and meta.

**The answer contract** ([prompts/answer_system.v1.txt](backend/app/llm/prompts/answer_system.v1.txt)).
Every QA pipeline asks for one JSON object, `{"answer": [...], "explanation": "..."}`:

- **`answer`**: fully-qualified names for code, repository-relative paths for files, names for people,
  and `#N` for PRs.
- **False premises**: if the question's premise is false, `answer` must be empty.
- **`explanation`**: concrete relationships stated in short sentences, so they can be checked.

Code tasks use [codegen_system.v1.txt](backend/app/llm/prompts/codegen_system.v1.txt) (one fenced Python
block). Replies are parsed leniently by [pipelines/parsing.py](backend/app/pipelines/parsing.py): it
accepts raw JSON, fenced JSON or the first balanced object; flattens dicts and comma-separated strings;
and falls back to the whole text as the explanation.

| Pipeline | What the model sees | Code |
|---|---|---|
| `none` | the question only. Anything right here came from memory (E3) | [no_retrieval.py](backend/app/pipelines/no_retrieval.py) |
| `vector` | the k = 8 nearest functions by vector search over `code_embedding`, as code chunks of up to 60 lines. The index is shared by all builds, so it is over-fetched (250, then 5,000 neighbours) and filtered to the build; if other builds still crowd this one out, an exact cosine scan of the build's functions takes over (`vector.similarity.cosine`, or numpy on Neo4j < 5.18) | [vector.py](backend/app/pipelines/vector.py), [store.py](backend/app/graph/store.py) `vector_search` |
| `graphrag` | seeds = symbols named in the question (backticks and dotted identifiers resolved through the BuildIndex; up to 3 matches for ambiguous names), plus 3 vector seeds when none are found or for localization. Each seed expands to its defining file, callers up to `graphrag_hops` (default **2**), callees (1 hop), class and sibling methods, inheritance in both directions, and the last 3 commits with author and PR. The subgraph is rendered as explicit edge lines `a -CALLS-> b [jedi, line 202]`, deduplicated and capped at 120 edges, plus the seed's code | [graphrag.py](backend/app/pipelines/graphrag.py) |
| `cypher_agent` | the graph schema and a JSON protocol: each turn is either `{"action":"query","cypher":...}` (rows come back, up to 30 shown) or `{"action":"answer",...}`. At most 4 queries. An agent that never answers is scored as an empty answer, not as a failed call | [cypher_agent.py](backend/app/pipelines/cypher_agent.py) |

**Fairness rules for code tasks.** The function being rewritten is "blanked":

- **vector** excludes it from the results.
- **graphrag** shows neither its code nor its outgoing calls, as if the body were deleted.
- **cypher_agent** does not run code tasks at all, because it could simply query the original code.

**Agent safety.** Agent queries are read-only in two ways: a regex rejects write clauses
(`CREATE`, `MERGE`, `SET`, `DELETE`, `LOAD CSV`, write procedures), ignoring string literals, and every
query runs in a Neo4j **READ transaction** with a 15 s timeout. Query errors are fed back to the model so
it can fix its Cypher.

**LLM gateway** (`LLMClient.complete`):

- **Routing**: LiteLLM sends a model id like `groq/llama-3.1-8b-instant`, `gemini/...`, `openai/...`,
  `anthropic/...` or `ollama/qwen2.5-coder:7b` to its provider (Ollama uses `OLLAMA_API_BASE`).
- **Parameters**: JSON mode (`response_format`) for QA, `drop_params` for providers that lack it,
  temperature 0, and retries.
- **Cost**: from `litellm.completion_cost`; 0 for local models.
- **Cache**: responses at temperature 0 are **cached on disk** (`llm_cache.sqlite`), keyed by model,
  messages and parameters. The cache stores the original latency and cost, so a cached rerun reports the
  same numbers and costs nothing.
- **Rate limits**: free tiers allow few requests and tokens per minute. Concurrent calls are capped per
  provider (`LLM_PROVIDER_CONCURRENCY`, default `groq=2,gemini=2,openrouter=2`, per process), and an HTTP 429
  is retried up to `LLM_RATE_LIMIT_RETRIES` times after the wait the provider asks for (its Retry-After, or
  "try again in 7.6s" in the message), else exponential backoff. A wait longer than
  `LLM_RATE_LIMIT_MAX_WAIT_S` (such as a used-up daily quota) fails the call at once instead. Reported
  latency is that of the successful attempt, without the waiting.
- **Mock models**: `mock/extractive` copies the sources of `a -CALLS-> b` lines whose target is named in
  the question (it knows only what retrieval showed it), and `mock/empty` always answers `[]`. They make
  every test and the CI smoke gate run offline.

**Tracing** ([llm/tracing.py](backend/app/llm/tracing.py)). Each answered question gets a trace id. JSONL
files under `<DATA_DIR>/traces/` always receive the retrieval (the exact context string and retrieved
nodes), every generation (messages, output, tokens, cost, latency, cache hit), every agent query, and
every score. With `LANGFUSE_PUBLIC_KEY/SECRET_KEY` set, generations reach Langfuse through LiteLLM's
`langfuse` callback under the same trace id, and retrieval spans and scores are sent with the Langfuse
SDK. One trace then holds the prompt, the retrieved subgraph and the scores.

### Step 6: Score

| | |
|---|---|
| Code | [eval/index.py](backend/app/eval/index.py), [normalize.py](backend/app/eval/normalize.py), [set_f1.py](backend/app/eval/set_f1.py), [claim_check.py](backend/app/eval/claim_check.py), [symbol_check.py](backend/app/eval/symbol_check.py), [judge.py](backend/app/eval/judge.py), [sandbox.py](backend/app/eval/sandbox.py), [scorer.py](backend/app/eval/scorer.py), [report.py](backend/app/eval/report.py) |
| Writes | `answers.parsed_items` (normalized), `answers.match` (raw, tp, fp, fn, invented), `claim_verdicts`, `symbol_checks`, `test_runs`, `scores` (one row per metric) |

**BuildIndex.** Scoring never queries Neo4j per claim. `load_build_index` reads the build once (functions
with parameters, classes with bases and attributes, files with namespaces, all CALLS/INHERITS edges with
resolvers, and the authors who modified each function) into an in-memory `BuildIndex`, which is cached per
build. The same class can be built from an in-memory `CodeGraph`, so the scorers are unit-tested with no
database.

**6a. Normalization** (`normalize.py`). A model may write the same function as `Session.send`,
`requests.sessions.Session.send()`, `src/requests/sessions.py::Session::send` or `def send`. Each predicted
item goes through four steps:

1. Clean it: strip backticks, quotes, `def`/`class`, and trailing `(...)` and `:`.
2. Turn pytest ids and `file.py::Class::method` into dotted names via the file's module.
3. Map it, in order: exact qualified name → a dotted **suffix that is unique in the repo** → a suffix that
   is unique **among the gold items**. The last rule is lenient: the question already pinned the name down.
4. If none of these matches, keep it as written. It then counts as a false positive.

Paths are matched by unique path suffix, and a dotted module name is accepted for its file. History
answers are split ("Ada in PR #12" becomes `#12` and `Ada`), and authors are mapped through `meta.aliases`.

**6b. List metrics.**

| Metric | Definition |
|---|---|
| `precision`, `recall`, `f1` | set-based, after normalization. Empty prediction for empty gold scores 1.0 (a correctly rejected false premise) |
| `exact_match` | F1 = 1 |
| `invented_item_rate` | share of predicted items that are not entities of the repo at all (hallucinated names) |
| `hit_at_1`, `hit_at_5` | localization: the answer list read as a ranking (E5) |
| `premise_rejected` | false premise: the answer list was empty |
| `context_recall` | share of gold items present among the nodes retrieval showed the model. With F1 it **separates retrieval errors from generation errors** (the CodeRAG-Bench idea): low context recall means retrieval missed; high context recall with low F1 means the model failed to use what it saw |

**6c. Claim checks on explanations** (`claim_check.py`, RefChecker-style). First, claims are extracted:

- **Regex rules** always run. They cover forms like "`a` calls `b` and `c`", "`x` is called by `y`",
  "`A` inherits from/extends/is a subclass of `B`", "`f` is defined in path.py", "`a` does not call `b`",
  "`t` tests `f`" and "`f` was last changed by Name".
- **An LLM extractor** ([prompts/claim_extract.v1.txt](backend/app/llm/prompts/claim_extract.v1.txt))
  also runs when the experiment has a `judge_model`.

Each claim's subject and object are then normalized to graph entities, and the claim gets a verdict with
an evidence string:

| Relation | supported | contradicted | unverifiable |
|---|---|---|---|
| `calls` | a precise CALLS edge exists. Evidence: `(a)-[:CALLS {line, resolved_by}]->(b)` | both functions exist, no edge, and `a` has no unresolved call with `b`'s name | unknown entity, only a name-matched edge, or `a` has an unresolved call that could be `b` |
| `not_calls` | no edge, and nothing unresolved could be it | a precise edge exists | name edge or a possible unresolved call |
| `inherits` | a precise INHERITS path exists | no path at all | only name-matched edges connect them |
| `defined_in` | the File DEFINES it | it is defined elsewhere (the evidence names the file) | unknown entity |
| `tests` | the test reaches it by a precise CALLS path of ≤ 3 hops | it does not | the subject is not a test |
| `modified_by` | an AUTHORED → MODIFIES path from that person (name, email or login) | the function has history, but not from that person | no history for it |

The scores are `claims`, `claim_precision` = supported / (supported + contradicted), and
`contradicted_claim_rate` = contradicted / claims. A contradicted calls-claim lists the subject's
unresolved calls in its evidence, so a human auditor can see when dynamic dispatch might be the reason.

**6d. Symbol checks on generated code** (`symbol_check.py`). The code is parsed with Python's `ast`. Its
environment combines the target module's stored namespace, the snippet's own imports, every local
binding, builtins, and `self`/`cls` typed as the target class. `x = RepoClass(...)` and calls to functions
with an inferred return class give instance types. Every repository symbol use is then checked, and the
problems follow the Delulu benchmark's categories:

| Category | Example |
|---|---|
| `missing_symbol` (invented API) | `from .utils import magic`, `utils.nonexistent(x)`, `self.speed` when no such attribute exists anywhere in the MRO (skipped when the class has external bases) |
| `wrong_arity` | too many positional arguments, a missing required argument, or a value given twice. `self`/`cls` are dropped for bound calls; constructors are checked against `__init__` |
| `unknown_keyword` | a keyword the function does not accept (and it has no `**kwargs`) |
| `undefined_name` | a name defined nowhere |
| `missing_import` | a top-level repo name used without importing it |
| `syntax_error` | the code does not parse |

Three rules keep correct code from being called a hallucination:

- **`self` may be a subclass.** Inside a base class or mixin, `self.url` is valid when a subclass defines
  `url`. For example, `RequestEncodingMixin.path_url` uses `self.url`, which `PreparedRequest` provides.
  This applies only to `self`/`cls`. A variable built as `x = RepoClass(...)` has exactly that class.
- **Object attributes exist.** Dunder attributes (`__dict__`, `__class__`, `__name__`...) are never missing.
- **The original's kind decides `self`.** The snippet replaces only the body, so a `@staticmethod` stays
  static even when the model omits the decorator. The scorer passes the target's kind from the graph, and
  the first parameter is not typed as `self` then.

**Self-audit.** Every build runs the checker over the repository's own non-test functions. They are correct
by construction, so whatever it flags is a checker false positive. The rate is stored as
`stats.symbol_audit` and shown on the build page. It bounds how often E4 blames a model for the checker's
blind spots. On psf/requests the rate went from 3.0 % (7 of 236 functions) before these rules to 0 %.
`verigraph audit-symbols BUILD` reruns it.

The scores are `syntax_ok`, `symbol_refs`, `missing_symbol_rate`, `wrong_arity_rate`, `undefined_names`
and `hallucinated` (1 if any problem). The repo functions the code calls are compared with the original's
callees as `callee_precision/recall/f1`; for code tasks `f1` is this callee F1.

**6e. LLM judge, only where no gold exists** (`judge.py`). With a `judge_model`, preferably from another
model family, the explanation's **faithfulness to the retrieved context** is measured Ragas-style: the
judge splits the explanation into statements and marks each as supported by the context or not. The score
is the supported fraction.

**6f. Execution (stretch goal)** (`sandbox.py`, `run_tests: true`). Per build, a Docker image
`verigraph-sandbox:<build_id>` is built once, with the repository and its requirements installed in
editable mode at `/repo`. For each code answer the steps are:

1. The checkout is copied.
2. The original function body is replaced (decorators and indentation are kept).
3. The copy is mounted at `/repo`.
4. Only the tests that reach the function (`meta.tests`, from the impact query) run, with
   `--network none`, 2 GB of memory and a timeout.

The result is `test_runs` and `test_pass_rate`. Generated code never runs outside this container.

**Aggregation** (`report.py`). `summarize` turns per-answer scores into an experiment summary:

- mean F1 with a **bootstrap 95 % CI**, F1 per family, and **F1 per hop depth on impact questions only**
  (`f1_by_hop`, the E2 curve). Inheritance questions record the depth of their deepest subclass, a different
  quantity. They used to share the curve and lifted its 1-hop point, so they now get their own
  `f1_by_inheritance_depth`;
- context recall, claim metrics, false-premise rejection and hit@k;
- codegen metrics, cost per question, latency (mean and median), and tokens;
- `errors` (failed model calls) and `f1_answered`, the mean F1 without them. `f1` counts a failed call as 0,
  so every row is over the same questions; the two together show a model's quality on a run that was
  rate-limited.

`leaderboard` lists every experiment on a dataset, and `compare` gives a **paired bootstrap** difference
between two experiments on the same questions, leaving out questions where either side's call failed.

### Step 7: Explore

The dashboard ([frontend/](frontend/)) is a Next.js app that calls the API from the browser
(`NEXT_PUBLIC_API_URL`).

| Page | Shows | API |
|---|---|---|
| `/` | register a repo and start a build (history, GitHub, embeddings, semantic model); builds and datasets with live status | `POST /repos`, `POST /repos/{id}/builds`, `GET /builds`, `GET /datasets` |
| `/builds/[id]` | stat tiles (including the symbol-checker false-positive rate); how call sites were resolved (precise, name, unresolved, external); edge-audit precision; dataset generator; **call-graph explorer**: symbol search, Cytoscape neighborhood (1-3 hops, focus at the centre, tests on the outer ring, dashed = name guess) next to Monaco with the function highlighted; `?symbol=` deep links; **Ask the graph** and **Change impact** panels ([4b](#4b-using-the-graph-directly-ask-and-change-impact)); **Delete build** | `/builds/{id}`, `/quality`, `/symbols`, `/neighborhood`, `/node`, `/file`, `POST /ask`, `POST /impact`, `DELETE /builds/{id}` |
| `/datasets/[id]` | experiment launcher (models, pipelines, judge, limit, concurrency, prompt version, GraphRAG hops, vector top-k, a family subset, sandbox tests); experiments with status; questions with gold answers and gold Cypher | `/datasets/{id}`, `/questions`, `POST /experiments` |
| `/datasets/[id]/leaderboard` | a model filter above two charts: **F1 by family** (E1, grouped bars) and **F1 by hop depth on impact questions** (E2, one line per pipeline); the full table (CI, per family, per hop, context recall, claim precision, contradictions, premise rejection, hit@5, code hallucination, cost, latency); **compare two experiments** (paired bootstrap: difference, 95 % CI, p, and whether the CI excludes 0); **checker agreement** (Cohen's kappa per checker, E6) | `/leaderboard`, `/leaderboard/hops`, `/compare`, `/datasets/{id}/agreement` |
| `/experiments/[id]` | summary tiles and per-question scores; a **Resume** button on a failed experiment, and **Retry failed calls** on a finished one with failed model calls | `/experiments/{id}/results`, `POST /experiments/{id}/resume` |
| `/answers/[id]` | question and gold; answer items as ✓ correct / ✕ wrong / ✕ not in the repo / ○ missed; explanation; **claim table with verdicts and evidence edges, each name linking into the graph explorer**; for code, Monaco with markers on lines that have symbol problems; agent queries; scores; human-label buttons | `/answers/{id}/checks`, `POST /answers/{id}/labels` |

The code views use Monaco **served by the dashboard itself**: `npm run dev`/`build` copy `monaco-editor`'s
AMD build to `public/monaco` first, so no CDN is contacted. The dashboard therefore also works offline and
behind strict proxies.

Colour is fixed per pipeline across every chart: no retrieval is blue, vector orange, GraphRAG green and
the Cypher agent yellow. These are the first four slots of a categorical palette validated for colour-blind
separation in light and dark mode. Status is always an icon plus a label, never colour alone. Every chart's
numbers also appear in the table.

### Step 8: Gate regressions

[.github/workflows/eval.yml](.github/workflows/eval.yml) runs four jobs:

| Job | When | What |
|---|---|---|
| `tests` | every push and PR | Neo4j service container, `ruff`, `pytest` (unit + integration), then the **offline smoke gate** ([evals/gate.smoke.yaml](evals/gate.smoke.yaml)): fixture repo, frozen 39-question set, `mock/extractive` over all four pipelines, thresholds such as "GraphRAG callers F1 ≥ 0.9, GraphRAG context recall ≥ 0.8", plus no more than 0.02 F1 drop against [baseline.smoke.json](evals/baseline.smoke.json). This catches retrieval regressions with no API key |
| `frontend` | every push and PR | `npm ci && npm run build` (includes the type check) |
| `changes` | every push and PR | path filter: did `backend/app/llm/prompts/**`, `backend/app/pipelines/**`, `backend/app/eval/**` or `evals/**` change? |
| `eval-gate` | when they changed, or on manual dispatch | **the plan's regression gate**: rebuild psf/requests at the pinned SHA with the same options, load the frozen **50-question** slice ([evals/datasets/requests-v2.32.3-gate.jsonl](evals/datasets/requests-v2.32.3-gate.jsonl)), run the configured model(s) on all pipelines, and fail on [thresholds.yaml](evals/thresholds.yaml) or on an F1 drop greater than `max_regression` against `baseline.json`. Skipped with a notice when the `GROQ_API_KEY` secret is missing (forks) |

`verigraph gate --config ... --update-baseline` records the current F1 per model|pipeline as the new
baseline. The thresholds in `thresholds.yaml` are deliberately loose until you calibrate them with a
first real run.

---

### 4b. Using the graph directly: Ask and Change impact

The plan gives the knowledge graph three jobs. Steps 4-6 cover two of them: writing the test set and grading
answers. The first job is answering developer questions such as "what breaks if I change
`parse_headers`?". Two features serve it on any finished build. They run synchronously in the API process
and write nothing to the results database.

**Ask the graph** (`POST /builds/{id}/ask`, `verigraph ask BUILD "question" --model M`,
[analysis/ask.py](backend/app/analysis/ask.py)). One pipeline (default GraphRAG) answers a free-form
question with the step-5 answer contract. A free question has no gold answer, so the graph grades what it
can:

- each answer item is resolved to a graph entity, and a code-like name that matches nothing is flagged
  **not in the repo**;
- each relationship in the explanation goes through the step-6c claim checker and is labelled supported,
  contradicted or unverifiable, with its evidence edge;
- the response also carries the retrieved context, the Cypher agent's queries, tokens, cost, latency and a
  trace id.

**Change impact** (`POST /builds/{id}/impact`, `verigraph impact BUILD --diff pr.patch`,
[analysis/impact.py](backend/app/analysis/impact.py)). This uses no LLM, so the answer is exact up to the
graph's coverage.

1. A unified diff is mapped to functions through its **old-side** line numbers (the SWE-bench patch
   reader). Build the graph at the commit the diff applies to, such as a PR's base. Function names can be
   given instead or as well; a class name stands for its constructor.
2. The precise CALLS edges are walked backwards up to `max_hops` (default 3) from the changed functions.
3. The output has four parts: the **affected callers** with their hop distance, the **tests to run** with
   pytest ids, the callers reachable **only through name-matched edges** (listed separately, never mixed
   in), and a Markdown summary for a pull-request comment that also states the build's unresolved-call rate.

On psf/requests, changing `utils.super_len` affects 7 callers within 3 hops and selects 16 tests.

A minimal CI recipe for posting the comment: build the graph at the PR's base SHA, then run
`git diff BASE...HEAD > pr.patch` and `verigraph impact BUILD --diff pr.patch --markdown impact.md`, and
post `impact.md` with `gh pr comment --body-file impact.md`.

## 5. Follow one question through the system

A real question from the psf/requests v2.32.3 dataset, answered by the offline `mock/extractive` model.
That model can only copy edges it is shown, which makes the difference between pipelines easy to see.

**1. The question and its gold answer** (step 4, `Callers` family). The generator picked
`requests.cookies.RequestsCookieJar._find_no_duplicates` from the seed query and ran the gold query:

```cypher
MATCH (caller:Function {build_id: $build_id})-[r:CALLS]->(:Function {build_id: $build_id, qualified_name: $qn})
WHERE r.resolved_by IN $precise AND NOT caller.is_test
RETURN DISTINCT caller.qualified_name AS gold
-- params: {build_id: 1, precise: ["scip","jedi","import"], qn: "requests.cookies.RequestsCookieJar._find_no_duplicates"}
```

> Which non-test functions call `RequestsCookieJar._find_no_duplicates` (defined in `src/requests/cookies.py`) directly?
>
> gold: `requests.cookies.RequestsCookieJar.get`, `requests.cookies.RequestsCookieJar.__getitem__`

The function's own docstring agrees: *"Both `__get_item__` and `get` call this function: it's never
used elsewhere in Requests."*

**2. Retrieval** (step 5). GraphRAG resolves the backticked name to the function, expands two hops of
callers, and sends this context, shown here shortened:

```text
Graph edges (resolved_by: scip/jedi/import are precise, name is a guess):
src/requests/cookies.py -DEFINES-> requests.cookies.RequestsCookieJar._find_no_duplicates
requests.cookies.RequestsCookieJar.get -CALLS-> requests.cookies.RequestsCookieJar._find_no_duplicates [jedi, line 202]
tests.test_requests.TestRequests.test_request_with_bytestring_host -CALLS-> requests.cookies.RequestsCookieJar.get [jedi, line 1204]
...
requests.cookies.RequestsCookieJar.__getitem__ -CALLS-> requests.cookies.RequestsCookieJar._find_no_duplicates [jedi, line 334]
requests.cookies.RequestsCookieJar -DEFINES-> requests.cookies.RequestsCookieJar._find_no_duplicates

Other methods of requests.cookies.RequestsCookieJar:
  def get(self, name, default=None, domain=None, path=None)
  ...
### requests.cookies.RequestsCookieJar._find_no_duplicates (src/requests/cookies.py:386-413)
<the function's code>
```

**3. Scoring** (step 6). The answer `["...RequestsCookieJar.__getitem__", "...RequestsCookieJar.get"]`
normalizes to the same two names, giving F1 1.0 and context recall 1.0. The two claims in the
explanation are both **supported**, with evidence such as
`(...RequestsCookieJar.get)-[:CALLS {line: 202, resolved_by: jedi}]->(..._find_no_duplicates)`.

**4. The other pipelines** on the same question: `none` answers `[]` (F1 0) and `vector` answers `[]`
(F1 0, context recall 0, because the nearest chunks did not include the callers). With a real model,
the `none` row is what it remembers about requests (E3), and the gap between `vector` and `graphrag` is
the graph's contribution (E1).

**5. On the leaderboard** (offline mock, 84 requests questions, 12 per family): GraphRAG context recall
is 0.96 against 0.25 for vector RAG (hash embedder). GraphRAG's F1 on impact questions is 0.79 at 1 hop,
0.14 at 2 hops and 0.20 at 3 hops (4 questions per hop, so the 2- and 3-hop values are within noise of each
other). That drop is the shape E2 is designed to expose: GraphRAG's default 2-hop expansion, copied by a
model that cannot reason further, misses most deeper answers. Before the hop curve was restricted to
impact questions, inheritance questions lifted it to 0.94 / 0.37 / 0.20.

## 6. Data model

### Neo4j

Every node has `build_id` (the build's graph key, `graph_builds.graph_id`) and `uid`. Constraints make
`uid` unique per label, and composite indexes on
`(build_id, name)`, `(build_id, qualified_name)` and `(build_id, path)` serve lookups. A full-text index
covers function and class names. The full schema is in [schema.cypher](backend/app/graph/schema.cypher).

| Node | Key properties |
|---|---|
| `Repo` | name, url, commit_sha |
| `Module` | name (dotted), path, is_package |
| `File` | path, module, is_test, is_package, loc, sha1, parse_errors, namespace_json |
| `Class` | qualified_name, name, module, file_path, bases, external_bases, decorators, start_line, end_line, docstring, attributes, is_test, is_nested, in_test_file |
| `Function` | qualified_name, name, module, file_path, class_qn, signature, params_json, param_names, decorators, start_line, def_line, end_line, docstring, code, kind, is_test, is_method, is_nested, is_async, pytest_id, returns, unresolved_calls, in_test_file, summary, summary_model, code_embedding, summary_embedding, embedding_model |
| `ExternalPackage` | name, is_stdlib |
| `Author` | name, email, login |
| `Commit` | sha, short_sha, title, message, authored_at, committed_at, order, is_merge, author_name |
| `PullRequest` | number, title, body, author, merged_at, url, source (git or github) |
| `Issue` | number, title, body, state, created_at, url, labels |
| `Feature` | name, description, model |

| Relationship | Properties |
|---|---|
| `(:Repo)-[:CONTAINS]->(:Module)-[:CONTAINS]->(:File)` | |
| `(:File)-[:DEFINES]->(:Class \| :Function)`, `(:Class)-[:DEFINES]->(:Function \| :Class)`, `(:Function)-[:DEFINES]->(:Function \| :Class)` | |
| `(:Function)-[:CALLS]->(:Function)` | line, lines, resolved_by (scip, jedi, import, name), ambiguity |
| `(:File)-[:IMPORTS]->(:Module \| :ExternalPackage)` | line |
| `(:Class)-[:INHERITS]->(:Class)` | resolved_by, position |
| `(:Function {is_test})-[:TESTS]->(:Function)` | resolved_by |
| `(:Author)-[:AUTHORED]->(:Commit)-[:MODIFIES]->(:Function)` | lines_added, lines_deleted, change_type |
| `(:PullRequest)-[:INCLUDES]->(:Commit)`, `(:PullRequest)-[:FIXES]->(:Issue)` | source |
| `(:Function)-[:IMPLEMENTS]->(:Feature)` | confidence (≤ 0.95), model, source = llm |

### PostgreSQL (results)

| Table | Key columns |
|---|---|
| `repos` | id, url, name, commit_sha (requested pin) |
| `graph_builds` | id, graph_id (the Neo4j key), repo_id, repo, commit_sha (resolved), parser_version, status, progress, options, stats (structure counts, resolver breakdown, `history`, `github`, `embeddings`, `summaries`, `features`, `symbol_audit`), unresolved_call_rate, checkout_path, error |
| `datasets` | id, build_id, version, status, progress, options, n_questions |
| `questions` | id, dataset_id, dataset_version, family, text, template_text, gold_cypher, gold_params, gold_answer, hop_count, target, meta |
| `experiments` | id, dataset_id, model, pipeline, prompt_version, judge_model, run_group, options, status, progress, summary |
| `answers` | id, experiment_id, question_id, text, parsed_items, match, explanation, code, retrieved_nodes, steps, latency_ms, prompt_tokens, completion_tokens, tokens, cost_usd, error, trace_id |
| `claim_verdicts` | answer_id, subject, relation, object, verdict, evidence_edge, extractor, claim_text |
| `symbol_checks` | answer_id, symbol, kind, exists, arity_ok, category, detail, line |
| `test_runs` | answer_id, tests_run, passed, failed, status, log_path |
| `scores` | answer_id, metric, value (long format: one row per metric) |
| `human_labels` | answer_id, claim_verdict_id (set for a per-claim label), label, notes |
| `edge_audits` | build_id, caller, callee, resolved_by, file_path, line, label |
| `semantic_audits` | build_id, function_qn, kind (summary or feature), content, judge_model, judge_label, human_label |

`PARSER_VERSION` ([backend/app/\_\_init\_\_.py](backend/app/__init__.py)) is stored on every build. Bump
it whenever parser or resolver output changes, because frozen datasets are only valid against graphs
built by the same version.

## 7. Trusting the graph and the scores

| Question | Mechanism | How |
|---|---|---|
| Is the parser right? | fixture repos with hand-known graphs | [tests/test_graph_build.py](backend/tests/test_graph_build.py) pins all 16 precise CALLS edges of `mini_repo` (aliases, re-exports, `super()`, inherited `self` calls, `Class(...).m()`, factories), its name edge, unresolved sites, INHERITS, TESTS, namespaces and a SCIP round trip |
| How precise is each resolver? | hand audit of sampled edges | `verigraph audit-edges BUILD --per-resolver 100` → fill in `label` in the CSV → `verigraph audit-score file.csv`; or `POST /builds/{id}/audit` + `PUT /audits/{id}`, shown on the build page |
| How much is left unresolved? | per-build rates | `unresolved_call_rate`, `name_only_rate`, and the resolution bar on the build page. Dynamic dispatch, callbacks and decorators are the usual gaps |
| Has the gold drifted? | re-derivation | `verigraph verify DATASET` |
| Does the symbol checker wrongly flag correct code? | self-audit on the repository's own code | computed at every build (`stats.symbol_audit`, a tile on the build page) or `verigraph audit-symbols BUILD`; anything flagged is a checker false positive |
| Are PR links real? | first-parent rule for merge titles | `stats.history.fork_merges_skipped` counts fork-internal merges that were not turned into PRs |
| Is the LLM layer faithful? | judge + humans | `verigraph audit-semantic BUILD --judge-model M --n 50` grades summaries and feature tags with a judge from another family, and writes a CSV for your own grading |
| Do the checkers agree with people? | Cohen's kappa | label answers and claims in the dashboard, then `GET /datasets/{id}/agreement` gives kappa and raw agreement for the claim checker, set-F1 exact match and the symbol checker (E6) |
| Is a difference real? | bootstrap | every F1 has a 95 % CI; `verigraph compare A B` or `GET /compare?a=&b=` gives a paired-bootstrap difference with a CI |

## 8. Running the six experiments

| # | Question | Run | Read |
|---|---|---|---|
| E1 | Which pipeline answers code questions best? | `verigraph run DS --models m1,m2,m3 --pipelines none,vector,graphrag,cypher_agent` | leaderboard: F1 per family, $/question, ms/question |
| E2 | Where does each pipeline break as reasoning gets deeper? | the same run (impact questions come balanced at 1/2/3 hops); try `--graphrag-hops 3` | the "F1 by hop depth" chart and the hop columns |
| E3 | How much comes from memory? | build psf/requests and your own repo; `--pipelines none` on both | the F1 gap between the two datasets; `invented_item_rate`, contradicted claims |
| E4 | Does graph context reduce API hallucinations? | the `codegen` family with `none,vector,graphrag` (add `--run-tests` for the sandbox) | code hallucination, missing-symbol and wrong-arity rates, callee F1, test pass rate |
| E5 | Does the graph help find the bug? | `localization` questions (needs GitHub issues: set `GITHUB_TOKEN`) or `verigraph swebench BUILD instances.jsonl` at an instance's base commit | `hit_at_1` / `hit_at_5` |
| E6 | Can the automatic checks be trusted? | label about 100 explanations in the answer view | `GET /datasets/{id}/agreement` |

## 9. Interfaces: HTTP API and CLI

| Method | Path | Does |
|---|---|---|
| POST | `/repos` | register a repo URL and pin a commit |
| GET | `/repos`, `/repos/{id}` | list / get |
| POST | `/repos/{id}/builds` | build the code graph (options: resolvers, history, github, max_commits, embeddings, embedding_model, semantic_model) |
| GET | `/builds`, `/builds/{id}` | status and stats |
| GET | `/builds/{id}/quality` | graph stats, unresolved-call rate, resolver breakdown, audit results |
| GET | `/builds/{id}/symbols?q=`, `/node?qn=`, `/neighborhood?qn=&depth=`, `/file?path=` | explorer data (file reads are confined to the checkout) |
| POST, GET | `/builds/{id}/audit`; PUT `/audits/{id}` | edge-audit sampling and labels |
| POST | `/builds/{id}/ask` | answer a free-form question with one pipeline; claims checked against the graph ([4b](#4b-using-the-graph-directly-ask-and-change-impact)) |
| POST | `/builds/{id}/impact` | changed functions → affected callers, tests to run, name-only callers, Markdown |
| DELETE | `/builds/{id}` | delete the build's graph, checkout, datasets, experiments, answers and labels (409 while a job runs) |
| POST | `/datasets` | generate questions from a build |
| GET | `/datasets`, `/datasets/{id}`, `/datasets/{id}/questions?family=`, `/datasets/{id}/export` (JSONL) | |
| POST | `/experiments` | run models × pipelines on a dataset |
| GET | `/experiments?dataset_id=`, `/experiments/{id}`, `/experiments/{id}/results` | summary and per-question scores |
| POST | `/experiments/{id}/resume` | re-enqueue a failed experiment, or a finished one with failed model calls (answered questions are skipped, failed calls asked again) |
| GET | `/leaderboard?dataset_id=`, `/leaderboard/hops?dataset_id=`, `/compare?a=&b=&metric=` | |
| GET | `/answers/{id}/checks` | claim verdicts, symbol checks, test runs, scores, labels |
| POST | `/answers/{id}/labels` | a human label for the answer or for one claim |
| GET | `/datasets/{id}/agreement` | Cohen's kappa per checker |
| GET | `/health` | database and Neo4j connectivity |

Interactive docs are at `http://localhost:8000/docs`.

| CLI (`verigraph ...`) | Does |
|---|---|
| `init-db` | create tables and the Neo4j schema |
| `build URL [--commit] [--resolvers] [--no-history] [--no-github] [--max-commits] [--embedding-model] [--semantic-model]` | steps 1-3 |
| `testgen BUILD [--families] [--per-family] [--seed] [--rephrase-model] [--version]` | step 4 |
| `freeze DATASET out.jsonl [--n 50]`, `load-dataset BUILD file.jsonl`, `verify DATASET` | frozen slices |
| `run DATASET --models ... [--pipelines] [--judge-model] [--limit] [--families] [--concurrency] [--run-tests] [--top-k] [--graphrag-hops]` | steps 5-6, then prints the leaderboard |
| `resume EXPERIMENT` | finish a failed or interrupted experiment, or re-ask its failed model calls |
| `report DATASET [--json]`, `compare A B [--metric]` | results |
| `audit-edges`, `audit-score`, `audit-semantic` | trust checks |
| `swebench BUILD instances.jsonl` | SWE-bench localization questions |
| `gate [--config] [--update-baseline]` | the CI regression gate |
| `ask BUILD "question" --model M [--pipeline] [--judge-model] [--json]` | developer question with graph-checked claims |
| `impact BUILD [--diff file] [-f function ...] [--hops] [--markdown out.md] [--json]` | change impact (no LLM) |
| `audit-symbols BUILD` | symbol-checker self-audit on the repository's own code |
| `delete-build BUILD [--yes]` | remove a build everywhere (Neo4j, disk, results database) |

## 10. Configuration

All settings are environment variables (or `.env`; see [.env.example](.env.example)), read by
[config.py](backend/app/config.py).

| Variable | Default | Meaning |
|---|---|---|
| `DATA_DIR` | `data` | mirrors, checkouts, LLM cache, traces, sandbox logs, local SQLite |
| `DATABASE_URL` | SQLite in `DATA_DIR` | e.g. `postgresql+psycopg://verigraph:verigraph@localhost:5432/verigraph` |
| `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD` | `bolt://localhost:7687`, `neo4j`, `verigraph-password` | |
| `JOB_BACKEND`, `REDIS_URL` | `inline` | `celery` sends jobs to Redis |
| `LOCAL_REPO_ROOTS` | empty (any path); Compose: `/fixtures,/repos` | folders that local-path repositories must live in. The API serves checkout files, so set it whenever the API is reachable by others |
| `RESOLVERS` | `scip,jedi,import,name` | resolver order |
| `GITHUB_TOKEN`, `MAX_HISTORY_COMMITS`, `MAX_PULL_REQUESTS` | –, 1000, 300 | |
| `EMBEDDING_MODEL`, `EMBEDDING_DIM`, `EMBEDDING_MAX_TOKENS` | `nomic-ai/CodeRankEmbed`, 768, 512 | `hash` needs no ML dependencies; raise the token cap on a GPU |
| `GROQ_API_KEY`, `GEMINI_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `OLLAMA_API_BASE` | | read by LiteLLM |
| `LLM_CACHE`, `LLM_TIMEOUT_S`, `LLM_NUM_RETRIES` | true, 120, 2 | |
| `LLM_PROVIDER_CONCURRENCY`, `LLM_RATE_LIMIT_RETRIES`, `LLM_RATE_LIMIT_MAX_WAIT_S` | `groq=2,gemini=2,openrouter=2`, 6, 60 | concurrent calls per provider (per process); waits after an HTTP 429 |
| `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_HOST` | | enable Langfuse |
| `SANDBOX_IMAGE_PREFIX`, `SANDBOX_TIMEOUT_S` | `verigraph-sandbox`, 600 | |

Per-experiment options (API `options`, or CLI flags): `top_k` (8), `graphrag_hops` (2),
`graphrag_max_edges` (120), `agent_max_queries` (4), `agent_max_rows` (30), `concurrency` (4), `limit`,
`families`, `run_tests`.

## 11. Running it

**Everything in Docker:**

```bash
cp -n .env.example .env       # preserve an existing configuration; see README for key-free mode
docker compose up -d --build  # neo4j :7474/:7687, postgres :5432, api :8000, web :3000
# optional: docker compose --profile tracing up -d   (Langfuse at :3001)
#           docker compose --profile ollama up -d    (then: docker compose exec ollama ollama pull qwen2.5-coder:7b)
```

Then open http://localhost:3000, register `https://github.com/psf/requests` at `v2.32.3`, build, generate
a dataset, and start an experiment.

**Local development:**

```bash
make neo4j                    # only Neo4j in Docker
make install                  # backend venv (+ dev tools) and frontend packages
cd backend
EMBEDDING_MODEL=hash .venv/bin/verigraph build https://github.com/psf/requests --commit v2.32.3
.venv/bin/verigraph testgen BUILD_ID --per-family 20
.venv/bin/verigraph run DATASET_ID --models groq/llama-3.1-8b-instant,mock/extractive
# Replace BUILD_ID and DATASET_ID with the numbers printed by the preceding commands.
# From the project root, in separate terminals:
# make api                    # API :8000; inline jobs
# make web                    # dashboard :3000
```

For CodeRankEmbed embeddings run `pip install -e '.[embeddings]'` (it pulls in torch; CPU wheels
work). Otherwise set `EMBEDDING_MODEL=hash`.

## 12. Tests and CI

`backend/tests/` includes unit tests and database integration tests:

| File | Covers |
|---|---|
| `test_parser.py` | definitions, qualified names, parameters and kinds, decorators, docstrings, imports, call sites, receivers, error tolerance, module naming, return/assignment tracking |
| `test_graph_build.py` | the fixture repo's full graph under both resolver chains, stats, namespaces, SCIP encode/decode/resolve, no infinite recursion on `x = x.copy()` |
| `test_history.py` | a scripted git repo (squash PR, merge-commit PR, "Fixes #3" in a branch commit): commit order, PR membership, FIXES, MODIFIES line counts; checkout pinning; a fork-sync history ("Merge pull request #1 from upstream/main" on a contributor branch) whose fork PR must not claim upstream commits |
| `test_eval.py` | set-F1 edge cases, normalization, claim extraction and verdicts, arity rules, symbol-checker categories, the scorer per family, kappa and bootstrap, function replacement |
| `test_llm_and_parsing.py` | JSON extraction, answer parsing, the read-only Cypher guard, prompts, mock models, cache and tracing |
| `test_integration.py` | **needs Neo4j** (skipped otherwise): writes the fixture graph with history, checks every family's gold against hand-computed answers, runs all four pipelines with mock models (including the blanking rule), runs the semantic layer, rephraser and judges against the mock, checks the overwrite guard, checks that vector search still returns k results when 300 functions of another build crowd the shared index, and drives the HTTP API end to end (including a redelivered dataset job, Ask, Impact, the self-audit and build deletion) |
| `test_gate.py` | thresholds, nested metrics and baseline regression |
| `test_api_validation.py` | invalid job input, missing embeddings, duplicate resume prevention, claim-label ownership, and the `LOCAL_REPO_ROOTS` allowlist (including `../` and `file://` escapes) |
| `test_analysis.py` | change impact on the fixture graph (diff → changed functions, precise callers with hop distances, tests with pytest ids, name-only callers kept apart, unmappable inputs), and the symbol-checker self-audit (0 false positives on the fixture) |
| `test_store.py` | the exact vector-search fallback without `vector.similarity.cosine`, and the read-only Cypher guard |

## 13. Design decisions and deviations from the plan

| Plan said | Built | Why |
|---|---|---|
| SCIP or Jedi first | both: `scip` is used automatically when `scip-python` is installed or an `index.scip` exists; otherwise Jedi | Jedi is pip-installable; scip-python needs Node. The SCIP reader decodes protobuf directly, with no generated bindings |
| PyDriller `changed_methods` | PyDriller for commits and diffs, but changed functions come from our own parser's line ranges | lizard's method names cannot be mapped onto graph qualified names reliably |
| PRs and issues from PyGithub | PR numbers come from commit messages first (works offline); PyGithub adds titles, bodies, issue texts and logins | builds work without a token, and the anonymous budget is spent where it matters |
| Seven question families | the seven, plus `codegen` (the plan's E4 tasks) as an eighth family; false-premise questions include one-third true controls | controls stop a model that always says "no" from scoring perfectly |
| Cypher agent with LangGraph or tool calling | a plain JSON protocol with any model | works with small and local models that lack tool calling |
| Ragas and DeepEval for free text | a Ragas-style faithfulness judge on our own LiteLLM gateway, and the CI gate as pytest-style thresholds | no LangChain dependency or version churn; exact checks come first, and the judge only scores what has no gold |
| Langfuse | self-hosted Langfuse v3 in a compose profile; SDK pinned to `<3` | LiteLLM's `langfuse` callback uses the v2 SDK API |
| Celery or Arq | Celery, plus an inline thread backend | jobs are synchronous; local development needs no Redis |
| (none) | a `hash` embedder and `mock/` models | tests and the CI smoke gate run with no network, GPU or API key |
| (none) | return-type inference and single-assignment local typing in the import resolver | makes `x = factory(); x.method()` precise without Jedi |
| (none) | `transformers<5` pinned in the `embeddings` extra | CodeRankEmbed's remote code calls an API that transformers 5 removed |

## 14. Known limitations

- **Python only.** Tree-sitter would allow other languages, but module naming, the resolvers and the
  symbol checker are Python-specific.
- **Resolution depends on the environment.** Jedi sees the installed packages, so the external versus
  unresolved split for third-party calls can differ slightly between machines. Repo-internal gold does
  not change.
- **Dynamic code.** Callbacks, `getattr`, decorators that wrap functions, monkeypatching and
  attribute-typed receivers (`self.adapter.send()`) often stay unresolved or name-only. Such questions are
  never asked from name edges. A claim about such a call can still be marked contradicted, which is why
  the evidence lists unresolved calls and why E6 measures agreement with humans.
- **History is matched by current names.** A renamed or moved function loses its older history; only the
  newest `MAX_HISTORY_COMMITS` commits are mined; and merge commits carry no modifications.
- **Localization gold is taken after the fix.** It is the set of functions the fixing PR modified, named
  as they exist at the pinned commit, which is after the fix. Use SWE-bench mode for the
  before-the-fix setting.
- **The symbol checker is flow-insensitive.** Its type inference is simple (constructors, inferred
  factory returns, `self`/`cls`). Methods on values of unknown type are not checked.
- **One embedding dimension per Neo4j.** The vector index is shared by all builds. Retrieval stays
  correct when other builds crowd the index, through the exact-scan fallback, but that scan costs one pass
  over the build's functions.
- **No authentication.** The API trusts its callers. `LOCAL_REPO_ROOTS` stops arbitrary server folders
  from being registered and read, but anyone who can reach the API can start builds and spend LLM credit.
  Keep it on localhost or behind an authenticating proxy.
- **Change impact is as complete as the call graph.** Callers through dynamic dispatch or callbacks are
  missing. Name-matched callers are listed separately, and the Markdown states the unresolved-call rate.
- **Agent build scoping relies on the prompt.** The agent is told to filter on `$build_id`. A query that
  forgets the filter can see other builds; that is the agent's mistake, and it shows up in the scores.
- **The sandbox** needs Docker access in the worker and a repository whose tests run after
  `pip install -e .` plus `requirements*.txt`.
- **Thresholds start as guesses.** The values in `evals/thresholds.yaml` are starting points; calibrate
  them with `--update-baseline`.

## 15. Extending the system

- **A new question family.** Add a `Family` subclass in [testgen/templates.py](backend/app/testgen/templates.py)
  with `seeds()` and `make()`, register it in `FAMILIES`, and if its answers are not code names add a
  normalization rule in [eval/normalize.py](backend/app/eval/normalize.py). Keep gold on precise edges.
- **A new pipeline.** Subclass `Pipeline` in [pipelines/](backend/app/pipelines/) (override `retrieve`,
  or `answer` for multi-turn pipelines), register it in `PIPELINES`, give it a colour slot in
  [frontend/lib/types.ts](frontend/lib/types.ts), and honour `meta.blank` for code tasks.
- **A new resolver.** Implement `resolve(site, fn) -> Resolution | None` in
  [graph/resolver.py](backend/app/graph/resolver.py) and add its name to `ALL_RESOLVERS` and, if it is
  precise, to `PRECISE_RESOLVERS` in [config.py](backend/app/config.py).
- **A new prompt version.** Copy `answer_system.v1.txt` to `v2`, run experiments with
  `prompt_version="v2"`, and compare them with `verigraph compare`. Old results stay comparable.
- **Another language.** Add a Tree-sitter grammar and a parser that emits the same `CodeGraph`
  dataclasses; for call resolution use `scip-typescript`, `scip-go` and so on through the SCIP resolver,
  whose decoder is language-independent.

## 16. Review log: what was corrected and verified

A full read of the code was followed by running every part of it, on the fixture and on psf/requests
v2.32.3, with the Docker services (Neo4j 5.26, PostgreSQL 16, Redis 7), the Celery worker, the API and
the production dashboard build. Each problem below was reproduced first, then fixed with a test that fails
on the old code.

| # | Problem found | Effect | Fix | Evidence |
|---|---|---|---|---|
| 1 | `Merge pull request #N` titles were trusted anywhere in history. A fork's own PR (`#1 from kennethreitz/master`, syncing upstream into the fork) became "PR #1 of requests" and claimed 274 commits | history gold could name the wrong PR; GraphRAG context and localization links could cite it | merge titles count only on the first-parent chain of the pinned commit ([3a](#step-3-add-history-and-meaning)) | requests: 19 fork merges skipped, commits in several PRs 164 → 3; `test_fork_sync_merges_do_not_become_pull_requests` |
| 2 | history gold picked a PR arbitrarily when the last commit sat in several PRs | a correct answer could score as wrong | PRs are aggregated per commit; with more than one, only the author is asked | `History` family in [templates.py](backend/app/testgen/templates.py) |
| 3 | the symbol checker flagged correct code: mixin attributes provided by subclasses, dunder attributes, and a staticmethod's first parameter typed as `self` | E4 "hallucination" rates blamed models for checker blind spots | the three rules in [6d](#step-6-score), plus a self-audit stored with each build | requests' own code: 3.0 % → 0 % flagged; `test_symbol_checker_has_no_false_positives_on_valid_code` |
| 4 | vector search over-fetched 250 neighbours from an index shared by all builds and silently returned fewer than k | vector RAG, and GraphRAG's vector seeds, degraded as more builds were added | wider over-fetch, then an exact per-build scan | 2 instead of 5 results before the fix; `test_vector_search_survives_other_builds_crowding_the_index` |
| 5 | the E2 "F1 by hop depth" curve mixed inheritance depth with impact hops | the 1- and 2-hop points were inflated (requests GraphRAG: 0.94 / 0.37 instead of 0.79 / 0.14) | impact only; inheritance gets `f1_by_inheritance_depth` | `test_hop_curve_uses_impact_questions_only` |
| 6 | a redelivered dataset job (Celery `acks_late` after a worker crash) appended a second copy of every question | duplicated questions in a dataset | the job replaces earlier attempts' questions | 12 instead of 6 questions before the fix; asserted in `test_api_flow` |
| 7 | any server folder could be registered as a "repository" and then read through `/builds/{id}/file` | arbitrary file read on the API host (the API has no authentication and Compose publishes it) | `LOCAL_REPO_ROOTS` allowlist, checked at registration and at build time | `test_local_repository_paths_are_confined_to_allowed_roots`, `test_api_rejects_local_paths_outside_the_roots` |
| 8 | Monaco was loaded from a CDN at runtime | code views failed offline and behind proxies (reproduced here) | Monaco copied into the dashboard's `public/` at build time | the browser check shows the editor rendering with no external request |
| 9 | codegen questions omitted decorators | a model could not know a method was `@staticmethod`/`@property` | decorators listed in the question | new datasets only; frozen sets are unchanged |
| 10 | smaller issues | SCIP temp directories leaked; a failed experiment kept paying for queued LLM calls; the CLI accepted unknown pipelines and a vector run on a build without embeddings; Rich swallowed `[supported]`-style text in CLI output; a failed API test left its build in Neo4j and broke the next run | fixed in place | covered by the suites above |

**Verified unchanged and correct.** The parser, symbol table and resolvers reproduce the documented requests
graph exactly: 36 files, 667 functions, 2,414 call sites, 12.6 % unresolved. Gold queries use precise
edges only. Every family's gold matches hand-computed answers. The offline gate passes.

**Environment notes from this run.** GitHub API enrichment was blocked by the network policy of the
machine used for the review (HTTP 403 from its proxy, not a code problem). The build recorded the error in
`stats.github` and continued, as designed. Real-model experiments were not run because no provider key was
configured, so every score above comes from the mock models and measures the plumbing, not model ability.

**Final check results:** 80 backend tests passed with Neo4j (68 before the review), Ruff passed, the
TypeScript check and the production dashboard build passed, the offline smoke gate passed, and the
API/Celery demo completed 8 experiments with no answer errors. The dashboard's Docker image could not be
rebuilt here because Docker Hub rate-limited the base-image pull (HTTP 429). Its only change, copying
`public/` into the runtime stage, was checked against the build output instead.

## 17. Roadmap: what to add next

In priority order: what turns the platform into findings first, then what extends it.

1. **Run the real experiments (E1-E6).** Run 3-4 real models (for example a small and a large open model on
   Groq or Ollama, plus one API model) on psf/requests and on a repository the models cannot have memorised.
   Then record the real gate's baseline (`verigraph gate --config ../evals/gate.yaml --update-baseline`;
   `evals/baseline.json` does not exist yet) and tighten `thresholds.yaml` from the measured values.
2. **Human labels for E6.** Label about 100 explanations and claims in the answer view. The agreement panel
   then reports kappa per checker with real data.
3. **SCIP in the Docker image.** Install `scip-python` (Node) in the backend image so the most precise
   resolver runs by default. Today Jedi resolves almost everything, and an edge audit per resolver would
   show whether SCIP adds precision.
4. **Authentication.** An API key or an OAuth proxy before the API is reachable by anyone else, plus a cost
   budget per experiment, so a run cannot exceed a set spend.
5. **Summary-vector and hybrid retrieval.** `summary_embedding` and its index are built but no pipeline reads
   them. A hybrid pipeline (code and summary vectors, then a graph expansion) is a natural fifth leaderboard
   row.
6. **An E3 view.** The same families on two datasets (a famous repo and your own) side by side, with the F1
   gap and the invented-item rate per pipeline.
7. **The PR impact comment as a GitHub Action**, using the recipe in [4b](#4b-using-the-graph-directly-ask-and-change-impact),
   and caching the base-commit build between runs.
8. **Releases and incremental builds.** Reuse per-file parse results between commits, then compare answers
   across two releases of the same repository (a stretch goal of the plan).
9. **More languages** through `scip-typescript` / `scip-go` and a Tree-sitter front end that emits the same
   `CodeGraph`.
