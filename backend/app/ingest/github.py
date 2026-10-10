"""Enrich the history layer from the GitHub API: PR titles/bodies, "fixes #123" links, issue texts, logins.

With a token (GITHUB_TOKEN) closed PRs are listed 100 per call and linked through merge_commit_sha. Without
one, only a small budget of single PR/issue lookups is spent (the anonymous limit is 60 calls per hour).
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from app.graph.model import CodeGraph, Issue, PullRequest
from app.ingest.history import iso_utc, parse_fixes

log = logging.getLogger(__name__)


class _Budget:
    def __init__(self, limit: int | None) -> None:
        self.limit, self.used = limit, 0

    def take(self) -> bool:
        if self.limit is not None and self.used >= self.limit:
            return False
        self.used += 1
        return True


def _apply_pr(p: PullRequest, pr, graph: CodeGraph) -> None:
    body = pr.body or ""
    p.title, p.body = pr.title, body[:4000]
    p.author = pr.user.login if pr.user else None
    p.merged_at, p.url, p.source = iso_utc(pr.merged_at), pr.html_url, "github"
    p.merge_commit_sha = pr.merge_commit_sha
    if pr.merge_commit_sha and pr.merge_commit_sha in graph.commits:
        p.commits.add(pr.merge_commit_sha)
    p.fixes |= parse_fixes(f"{pr.title}\n{body}")


def enrich_from_github(graph: CodeGraph, slug: str, token: str | None, max_prs: int, anonymous_budget: int,
                       progress: Callable[[str], None] | None = None) -> dict:
    from github import Auth, Github, GithubException, RateLimitExceededException

    say = progress or (lambda _m: None)
    gh = Github(auth=Auth.Token(token), per_page=100) if token else Github(per_page=100)
    budget = _Budget(None if token else anonymous_budget)
    stats = {"prs_enriched": 0, "issues": 0, "logins": 0, "api_calls_capped": False}
    try:
        budget.take()
        repo = gh.get_repo(slug)
        by_merge_sha = {}
        if token:
            say("github: listing merged pull requests")
            for i, pr in enumerate(repo.get_pulls(state="closed", sort="updated", direction="desc")):
                if i >= max_prs:
                    break
                if pr.merged_at and pr.merge_commit_sha in graph.commits:
                    by_merge_sha[pr.number] = pr
            for number, pr in by_merge_sha.items():
                _apply_pr(graph.pull_requests.setdefault(number, PullRequest(number=number)), pr, graph)
                stats["prs_enriched"] += 1
            say("github: mapping commit authors to logins")
            for i, commit in enumerate(repo.get_commits(sha=graph.commit_sha) if graph.commit_sha else []):
                if i >= len(graph.commits):
                    break
                if commit.author is not None and commit.commit.author and commit.commit.author.email:
                    graph.author_logins[commit.commit.author.email.lower()] = commit.author.login
            stats["logins"] = len(graph.author_logins)

        # Without a token, keep ~40% of the budget for issue texts: they are what localization questions need.
        pr_budget = _Budget(None if token else int(anonymous_budget * 0.6))
        for number in sorted(graph.pull_requests, reverse=True)[:max_prs]:
            p = graph.pull_requests[number]
            if p.source == "github":
                continue
            if not pr_budget.take() or not budget.take():
                stats["api_calls_capped"] = True
                break
            try:
                _apply_pr(p, repo.get_pull(number), graph)
                stats["prs_enriched"] += 1
            except GithubException as exc:
                log.debug("PR #%s: %s", number, exc)

        wanted = sorted({n for p in graph.pull_requests.values() for n in p.fixes}, reverse=True)
        say(f"github: fetching {len(wanted)} linked issues")
        for number in wanted:
            if not budget.take():
                stats["api_calls_capped"] = True
                break
            try:
                issue = repo.get_issue(number)
            except GithubException as exc:
                log.debug("issue #%s: %s", number, exc)
                continue
            if issue.pull_request is not None:  # "fixes #N" pointed at another PR
                continue
            graph.issues[number] = Issue(
                number=number, title=issue.title, body=(issue.body or "")[:4000], state=issue.state,
                created_at=iso_utc(issue.created_at), url=issue.html_url, labels=[lb.name for lb in issue.labels],
            )
            stats["issues"] += 1
    except RateLimitExceededException:
        log.warning("GitHub rate limit reached; history is partially enriched")
        stats["api_calls_capped"] = True
    except GithubException as exc:
        log.warning("GitHub enrichment failed for %s: %s", slug, exc)
        stats["error"] = str(exc)
    return stats
