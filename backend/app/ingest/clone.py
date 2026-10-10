"""Get a repository onto disk at a pinned commit.

Remote repos are mirrored once into `<data_dir>/mirrors` and each build gets its own clone from that
mirror, so rebuilding at another commit needs no network and builds never share a working tree.
Local git repos are cloned too (the user's working copy is never touched); plain folders are copied
and have no history.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from git import GitCommandError
from git import Repo as GitRepo

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


def checkout_repo(url: str, commit: str | None, dest: Path, mirrors_dir: Path) -> Checkout:
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
        mirrors_dir.mkdir(parents=True, exist_ok=True)
        mirror = mirrors_dir / ((slug or repo_name(url)).replace("/", "__") + ".git")
        if mirror.exists():
            GitRepo(mirror).git.remote("update", "--prune")
        else:
            GitRepo.clone_from(url, mirror, mirror=True)
        source = str(mirror)

    repo = GitRepo.clone_from(source, dest, no_checkout=True)
    target = commit or repo.head.commit.hexsha
    try:
        repo.git.checkout(target)
    except GitCommandError:
        repo.git.fetch("origin", target)  # e.g. a commit only reachable from a PR ref
        repo.git.checkout("FETCH_HEAD")
    return Checkout(dest, repo.head.commit.hexsha, True, slug)
