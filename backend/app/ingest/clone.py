"""Get a repository onto disk at a pinned commit.

Remote repos are mirrored once into `<data_dir>/mirrors` and each build gets its own clone from that
mirror, so rebuilding at another commit needs no network and builds never share a working tree. Builds of
the same repository take turns on its mirror (a file lock), and network failures are retried.
Local git repos are cloned too (the user's working copy is never touched); plain folders are copied
and have no history.
"""

from __future__ import annotations

import fcntl
import logging
import re
import shutil
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from git import GitCommandError
from git import Repo as GitRepo

log = logging.getLogger(__name__)

FETCH_ATTEMPTS = 3  # tries to clone or update a mirror before the build fails
RETRY_WAIT_S = 10.0  # times the attempt number
_HTTP11 = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.version", "GIT_CONFIG_VALUE_0": "HTTP/1.1"}
_GITHUB = re.compile(r"github\.com[/:]([\w.-]+)/([\w.-]+?)(?:\.git)?/?$")
_SCP_LIKE = re.compile(r"^[\w.-]+@[\w.-]+:")  # git@github.com:owner/name.git
_COPY_IGNORE = shutil.ignore_patterns(".git", "node_modules", ".venv", "venv", "__pycache__", ".tox")


@dataclass
class Checkout:
    path: Path
    commit_sha: str | None  # None for a plain folder without git
    has_history: bool
    github_slug: str | None  # "owner/name" when the repo lives on GitHub


def github_slug(url: str) -> str | None:
    m = _GITHUB.search(url.strip())
    return f"{m.group(1)}/{m.group(2)}" if m else None


def local_source(url: str) -> Path | None:
    """The folder a repository URL points to on this machine, or None for a remote URL."""
    url = url.strip()
    if url.startswith("file://"):
        return Path(url.removeprefix("file://")).expanduser()
    if "://" in url or _SCP_LIKE.match(url):
        return None
    return Path(url).expanduser()


def local_source_allowed(url: str, roots: list[Path]) -> bool:
    """True for remote URLs, and for local paths inside one of `roots` (any local path when `roots` is empty)."""
    path = local_source(url)
    if path is None or not roots:
        return True
    resolved = path.resolve()
    return any(resolved == root or resolved.is_relative_to(root) for root in roots)


def repo_name(url: str) -> str:
    slug = github_slug(url)
    if slug:
        return slug
    return Path(url.rstrip("/")).name.removesuffix(".git") or "repo"


def checkout_repo(url: str, commit: str | None, dest: Path, mirrors_dir: Path,
                  *, history: bool = True) -> Checkout:
    local = Path(url).expanduser()
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    if local.exists() and not (local / ".git").exists():
        shutil.copytree(local, dest, ignore=_COPY_IGNORE)
        return Checkout(dest, None, False, None)

    if local.exists():
        source = str(local.resolve())
        slug = None
        try:
            origin = GitRepo(source).remotes.origin.url
            slug = github_slug(origin)
        except (AttributeError, ValueError):
            pass
    else:
        slug = github_slug(url)
        if not history:
            return _snapshot_checkout(url, commit, dest, slug)
        mirrors_dir.mkdir(parents=True, exist_ok=True)
        mirror = mirrors_dir / ((slug or repo_name(url)).replace("/", "__") + ".git")
        with _mirror_lock(mirror):
            _sync_mirror(url, mirror)
            return _checkout(str(mirror), commit, dest, slug)
    return _checkout(source, commit, dest, slug)


def _snapshot_checkout(url: str, commit: str | None, dest: Path, slug: str | None) -> Checkout:
    """Fetch only the requested snapshot for exploration, without downloading all branches/history."""
    for attempt in range(1, FETCH_ATTEMPTS + 1):
        try:
            repo = GitRepo.init(dest)
            repo.create_remote("origin", url)
            with repo.git.custom_environment(**_HTTP11):
                repo.git.fetch("--depth=1", "origin", commit or "HEAD", kill_after_timeout=180)
                repo.git.checkout("--detach", "FETCH_HEAD")
            return Checkout(dest, repo.head.commit.hexsha, False, slug)
        except GitCommandError:
            shutil.rmtree(dest, ignore_errors=True)
            if attempt == FETCH_ATTEMPTS:
                raise
            log.warning("snapshot checkout failed; retrying (%d/%d)", attempt, FETCH_ATTEMPTS)
            time.sleep(RETRY_WAIT_S * attempt)
    raise RuntimeError("snapshot checkout failed")


@contextmanager
def _mirror_lock(mirror: Path) -> Iterator[None]:
    """Every build of a repository shares its mirror, so one at a time updates it and clones from it. Without
    this, a second build updated the half-written mirror of a first, which then deleted it on a failed clone."""
    with open(mirror.with_name(mirror.name + ".lock"), "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)  # released when the file closes, also if the process dies
        yield


def _sync_mirror(url: str, mirror: Path) -> None:
    """Clone or update the mirror, retrying network failures. Retries use HTTP/1.1, because GitHub can reset an
    HTTP/2 stream during a long transfer ("curl 92 ... was not closed cleanly", "early EOF")."""
    for attempt in range(1, FETCH_ATTEMPTS + 1):
        existed = mirror.exists()
        env = _HTTP11 if attempt > 1 else None
        try:
            if existed:
                repo = GitRepo(mirror)
                with repo.git.custom_environment(**(env or {})):
                    repo.git.remote("update", "--prune")
            else:
                GitRepo.clone_from(url, mirror, mirror=True, env=env)
            return
        except GitCommandError as exc:
            if attempt == FETCH_ATTEMPTS:
                raise
            if not existed:  # never retry on top of a partial clone
                shutil.rmtree(mirror, ignore_errors=True)
            log.warning("fetching %s failed (attempt %d of %d), retrying over HTTP/1.1: %s", url, attempt,
                        FETCH_ATTEMPTS, str(exc.stderr or exc).strip()[-300:])
            time.sleep(RETRY_WAIT_S * attempt)


def _checkout(source: str, commit: str | None, dest: Path, slug: str | None) -> Checkout:
    repo = GitRepo.clone_from(source, dest, no_checkout=True)
    target = commit or repo.head.commit.hexsha
    try:
        repo.git.checkout(target)
    except GitCommandError:
        repo.git.fetch("origin", target)  # e.g. a commit only reachable from a PR ref
        repo.git.checkout("FETCH_HEAD")
    return Checkout(dest, repo.head.commit.hexsha, True, slug)
