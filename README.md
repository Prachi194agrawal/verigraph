# VeriGraph for Code

Turn a mixed-language repository into a **code knowledge graph**, then use the graph to **test and grade LLMs** on
that codebase. The graph comes from parsers and static resolvers. Cypher provides reproducible reference
answers about the analyzed snapshot; missing graph relationships can remain unverifiable because static
analysis does not capture every runtime behavior.

- **Structure**: Python import/inference analysis plus Tree-sitter adapters for common languages; files,
  classes, functions, imports and conservatively resolved calls. Every file and symbol records its language
  and analysis level. Unrecognized text sources remain a file inventory, with no fabricated calls.
- **History** (git + GitHub): commits, authors, PRs and issues, linked to the functions they touched.
- **Meaning** (LLM, optional): one-line summaries and feature tags; code embeddings for vector search.

The output is a leaderboard of **models × pipelines** (no retrieval, vector RAG, GraphRAG, Cypher agent)
on your own codebase, with reference answers, retrieved evidence, and supported or unverifiable claims.

The graph also answers developers directly:

- **Ask the graph**: a free-form question answered by any pipeline. Every name and claim in the answer is
  checked against the graph.
- **Change impact**: a diff or a list of functions gives observed affected callers and suggested tests,
  with no LLM. It can produce Markdown for a PR comment.

**How it all works, step by step: [architecture.md](architecture.md).**

## Start here: understand a repository before contributing

1. Start the services and open http://localhost:3000.
2. Paste a GitHub URL, leave the commit blank for the default branch, and choose **Fast** analysis.
   History, GitHub enrichment, and LLM summaries are optional and off initially.
3. Open the finished build. Browse suggested functions or files; inspect the source, callers and graph edges.
4. Use **Change impact** for a function or a diff against the analyzed commit. Run the suggested tests yourself.
5. For explanations, configure a provider key in `.env` and choose a real supported model in **Ask**.
6. Evaluation is optional: generate questions, run the same real model with different retrieval pipelines,
   and inspect each answer against the captured graph. Mock models only check the software workflow.

**What language support means:** Python has its existing import/Jedi/SCIP analysis (Jedi/SCIP are selected
in Thorough mode). JavaScript, TypeScript/TSX, Java, Go, Rust, C/C++, C#, Ruby, PHP, Kotlin, Swift, Scala,
Lua and Bash have structural adapters and conservative scope/import resolution. Other detected source,
configuration and documentation files are inventoried and can be browsed; they do not receive equivalent
semantic analysis. The dashboard reports each build's actual coverage. Cross-language API calls, unknown
receiver types, dynamic dispatch, and framework callbacks are not fully modeled. Generated-code checking
and execution remain Python-only. `/capabilities` lists language levels and provider configuration without
returning secrets.

Fast mode uses shallow remote snapshots when history is disabled and avoids expensive Python inference.
Thorough mode trades time for more Python call inference. Build pages show the measured structure time;
there is no universal build-time guarantee. Hash embeddings are the lightweight default; opt into neural
embeddings when measuring semantic retrieval.

## Deployment and PR review

For a shared server, use the [secure deployment guide](docs/deployment.md): HTTPS through Caddy,
API authentication, optional read-only public samples, durable per-user write quotas, provider-call
budgets, repository limits and backups. This is a shared installation, with no tenant isolation.
The [PR review guide](docs/pr-review.md) covers the offline `verigraph pr-review` command and the GitHub
workflow that posts source-linked impact comments for same-repository PRs and artifacts for forks.

## First setup: no API keys needed

Requirements: Git, Docker with Compose v2.24+ and a running Docker daemon; Python 3.11+ for the demo
script. The frontend uses Node 22 inside Docker, so installing Node on your host is optional.
Run commands from the folder containing this README and `docker-compose.yml`.

```bash
cp -n .env.example .env
```

The example configuration already selects a small CPU-only setup:

```dotenv
INSTALL_EMBEDDINGS=false
EMBEDDING_MODEL=hash
```

Then start and verify the services:

```bash
docker compose config --quiet
docker compose up -d --build
docker compose ps
curl http://localhost:8000/health
python3 scripts/demo.py
```

Wait until health reports both `database: ok` and `neo4j: ok` before running the demo. The script uses
the bundled fixture, waits for the actual Celery jobs, generates questions, runs two mock models across
all four pipelines, checks for answer errors, and prints the leaderboard URL. It uses returned IDs,
so you can run it more than once. New runs use versioned v2 multilingual answer prompts. It needs no provider key, model download, or remote repository.
Building the Docker images for the first time still requires internet access.
The fixture has no git history, so history/localization questions are absent; it demonstrates the
structure, retrieval, grading and dashboard flow. Mock scores are a software check, not model findings.

Try the bundled mixed-language example without a remote download:

```bash
python3 scripts/demo.py --repo /fixtures/mixed_repo
```

It contains Python, JavaScript, TypeScript, Go and Rust functions and a Dart source file with inventory-only
coverage. Open the resulting build to see the coverage report and browse each language's source.

Dashboard: http://localhost:3000. API docs: http://localhost:8000/docs. Neo4j browser:
http://localhost:7474 (user `neo4j`, password from `NEO4J_PASSWORD`).

## Which API keys are needed?

**Only the key for the provider you choose is required.** Put keys in the root `.env`, never in the
frontend. The same provider key supports answers, optional summaries/features, question rephrasing,
and optional judging. A judge from another provider needs that provider's key too.

| Use | Variable | Where to obtain it / notes |
|---|---|---|
| Mock demo | none | `mock/extractive` and `mock/empty` are test doubles |
| Local Ollama models | none | start the `ollama` Compose profile and pull the chosen model |
| Groq | `GROQ_API_KEY` | [Groq console](https://console.groq.com/keys); model names start with `groq/` |
| Gemini | `GEMINI_API_KEY` | [Google API-key guide](https://ai.google.dev/gemini-api/docs/api-key); model names start with `gemini/` |
| OpenAI | `OPENAI_API_KEY` | your OpenAI API project key; model names use the LiteLLM OpenAI provider |
| Anthropic | `ANTHROPIC_API_KEY` | your Anthropic API key; model names start with `anthropic/` |
| OpenRouter | `OPENROUTER_API_KEY` | [OpenRouter keys](https://openrouter.ai/keys); model names start with `openrouter/`, and those ending in `:free` cost nothing |
| GitHub issues and PR enrichment | `GITHUB_TOKEN` (optional for public repos) | increases API allowance; it is separate from model access |
| Tracing | `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` (optional) | keys from your Langfuse project; JSONL tracing already works without them |
| Embeddings | none for `hash` or the default public CodeRankEmbed model | neural embeddings download model weights |

After editing provider keys, recreate the API and worker so they receive the new environment:

```bash
docker compose up -d --force-recreate api worker
```

## Run on a real repository

For a first evaluation, add `GROQ_API_KEY` to `.env`, then use:

```bash
python3 scripts/demo.py \
  --repo https://github.com/psf/requests \
  --commit v2.32.3 \
  --history --github \
  --models groq/llama-3.1-8b-instant \
  --semantic-model groq/llama-3.1-8b-instant \
  --per-family 10
```

Provider model availability changes; use a supported LiteLLM model name for your account. This example
caps history at 100 commits and semantic enrichment at 20 functions to keep the first run manageable.
Omit `--semantic-model` to skip summaries and feature tags. For a research benchmark, build through
the dashboard/CLI with your chosen full limits, freeze the dataset, and compare real models.

For local Ollama:

```bash
docker compose --profile ollama up -d
docker compose exec ollama ollama pull qwen2.5-coder:7b
python3 scripts/demo.py --models ollama/qwen2.5-coder:7b
```

For neural code embeddings, set `INSTALL_EMBEDDINGS=true` and
`EMBEDDING_MODEL=nomic-ai/CodeRankEmbed` in `.env`, rebuild, and pass
`--embedding-model nomic-ai/CodeRankEmbed` to the demo. Hash embeddings are lexical; neural embeddings
are needed when studying semantic vector retrieval.

## Ask questions and analyse change impact

On a finished build's dashboard page, use the **Ask the graph** and **Change impact** panels, or the CLI:

```bash
cd backend
.venv/bin/verigraph ask 1 "Which tests could fail if \`Session.send\` changes?" --model groq/llama-3.1-8b-instant
.venv/bin/verigraph impact 1 --function Session.send --hops 2
git -C /path/to/repo diff BASE...HEAD > pr.patch   # build the graph at BASE first
.venv/bin/verigraph impact 1 --diff pr.patch --markdown impact.md
```

`mock/extractive` works for `ask` without a key. `verigraph delete-build ID`, or the **Delete build** button,
removes a build's graph, checkout and results.

## Diagnose startup and jobs

```bash
docker compose logs --tail=100 api worker web
docker compose down
```

`down` stops services and keeps stored data. If a port is occupied, resolve that conflict before
starting. Inside Docker, a repository path must exist in the worker container; your host's home path
is not automatically available. Prefer a Git URL, or bind-mount the folder read-only under `/repos` in
both backend services. Local paths are accepted only inside `LOCAL_REPO_ROOTS` (Compose default:
`/fixtures,/repos`), because the API serves files from build checkouts. Local development is unauthenticated
by default; use the [production configuration](docs/deployment.md) before exposing it remotely.
Generated-code execution is optional and requires a separately configured Docker-capable worker;
the default Compose setup runs static symbol checks.

## Quick start (Docker)

```bash
cp .env.example .env              # add a model key, e.g. GROQ_API_KEY (optional: GITHUB_TOKEN)
docker compose up -d --build      # neo4j, postgres, redis, api :8000, worker, dashboard :3000
```

Open http://localhost:3000, register `https://github.com/psf/requests` at commit `v2.32.3`, build the
graph, generate a dataset, and run models such as `groq/llama-3.1-8b-instant`. The models `mock/extractive`
and `mock/empty` run offline. Optional profiles: `--profile tracing` (self-hosted Langfuse on :3001) and
`--profile ollama` (local models).

## Quick start (local, CLI)

```bash
make neo4j && make install
export EMBEDDING_MODEL=hash  # local install does not include neural embedding dependencies
cd backend
.venv/bin/verigraph build https://github.com/psf/requests --commit v2.32.3   # steps 1-3
.venv/bin/verigraph testgen 1 --per-family 20                                # step 4
.venv/bin/verigraph run 1 --models groq/llama-3.1-8b-instant \
    --pipelines none,vector,graphrag,cypher_agent                            # steps 5-6
.venv/bin/verigraph report 1
```

Use `EMBEDDING_MODEL=hash` if you don't want to install the `embeddings` extra (torch +
CodeRankEmbed). `make api` and `make web` start the API and dashboard against the same data.

## Tests and the eval gate

```bash
make test         # integration tests need Neo4j (make neo4j)
make gate-smoke   # offline gate: fixture repo + mock model + thresholds
make gate         # real gate: frozen 50-question psf/requests slice (needs GROQ_API_KEY)
```

CI ([.github/workflows/eval.yml](.github/workflows/eval.yml)) runs the tests and the smoke gate on every
push, and runs the real gate on pull requests that change prompts, pipelines or evals.

Local CLI runs (SQLite results database) and the Docker stack (PostgreSQL) can share one Neo4j: each
build stores its graph under its own random key (`graph_id`, shown on the build page), so their build
numbers never collide.

```bash
make install
make lint
make test
make gate-smoke
```

Add the repository Actions secret `GROQ_API_KEY` to enable the real provider gate; `.env` is not
uploaded to GitHub and CI cannot read it. The provider gate skips when this secret is absent.

Multilingual validation on 2026-10-11: the backend suite, Ruff, TypeScript check, production frontend
build and existing offline evaluation gate passed. Live Docker/API/Celery runs completed the bundled
mixed-language example across all four pipelines and a fresh `Prachi194agrawal/ianvs` snapshot
(`b7af1863bb5b`) with GraphRAG, without answer errors. The Ianvs build took about 64 seconds overall,
including 5.8 seconds of parsing/call resolution; this is one machine's measurement with hash embeddings,
not a performance guarantee. Live browser checks covered source browsing, questions and leaderboard.
Model calls used mocks; real provider quality and runtime call-graph completeness were not established.

Earlier Python-only validation on 2026-10-10, before the multilingual changes, after the review in [architecture.md section 16](architecture.md#16-review-log-what-was-corrected-and-verified):
**80 backend tests passed with Neo4j**, Ruff passed, the TypeScript check and frontend production build
passed, and the offline gate passed. The API/Celery demo completed eight experiments (two mock models ×
four pipelines) without answer errors, and psf/requests v2.32.3 was built, questioned and scored through
the API, the Celery worker and PostgreSQL. Real provider credentials and human research labels were not
used in these checks. [Section 17](architecture.md#17-roadmap-what-to-add-next) lists what to add next.
