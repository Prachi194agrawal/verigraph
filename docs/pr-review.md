# Evidence-backed PR impact review

The reviewer uses the existing multilingual parser and precise static call graph. It identifies changed
functions, their reachable callers and tests, and reports name-only guesses separately. Source references
link to immutable base-commit files and function line numbers. This is impact analysis, not a claim that
the PR is correct: it does not run tests, import target modules, invoke an LLM or infer runtime coverage.

## Run locally

Install the backend and fetch the PR's base and head commits into a local Git repository, then run:

```bash
verigraph pr-review --repo /path/to/repository --base main --head feature \
  --github-repo owner/repository --markdown impact.md --json-output impact.json
```

The command computes the merge base, parses an isolated archive of that snapshot, and examines the diff
from that merge base to the PR head. It needs no Neo4j, PostgreSQL or model key. It does not modify the
working tree. The Markdown is suitable for a PR comment; JSON includes the complete impact and evidence.
Use `--hops 1` through `--hops 6` to change traversal depth (default: 3).

New files cannot map to base functions and are listed as unverifiable. Module-level changes and files
outside the parsed graph are surfaced as unmapped inputs. Dynamic dispatch, callbacks and cross-language
calls may be absent, so an empty test list does not mean no tests are needed. Function source references
identify definitions; they do not independently verify every runtime call path. The reviewer uses import
and conservative name resolution; guessed edges are excluded from the precise impact set.

## GitHub automation

`.github/workflows/pr-review.yml` runs on opened, updated and reopened PRs after it has been merged into
the default/base branch. Analysis installs the trusted base-branch analyzer, uses a read-only token,
and uploads `impact.md` and `impact.json` as a 14-day artifact. It does not execute proposed PR code.

For same-repository PRs, a separate job with no source checkout and only PR-comment permissions downloads
the artifact and creates or updates one marked bot comment. It checks the current head SHA so a stale
run cannot replace the latest report. Fork PRs retain the downloadable artifact and receive no automatic
comment, because their token is read-only. No `pull_request_target` workflow or provider secret is used.
Repository Actions settings must allow `GITHUB_TOKEN` to write pull-request comments for the comment job.

Automatic publication will happen on future PR events once this workflow is enabled on GitHub; this
implementation does not post a comment from your current development session.
