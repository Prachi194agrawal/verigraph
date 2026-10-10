from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
MINI_REPO = FIXTURES / "mini_repo"


@pytest.fixture(scope="session")
def mini_repo() -> Path:
    return MINI_REPO


@pytest.fixture(scope="session")
def mini_graph():
    """The fixture repo resolved with Jedi first (the default chain)."""
    from app.graph.builder import build_structure

    return build_structure(MINI_REPO, "mini", str(MINI_REPO), None, ["jedi", "import", "name"])


@pytest.fixture(scope="session")
def mini_graph_import_only():
    from app.graph.builder import build_structure

    return build_structure(MINI_REPO, "mini", str(MINI_REPO), None, ["import", "name"])


@pytest.fixture(scope="session")
def mini_index(mini_graph):
    from app.eval.index import BuildIndex, index_rows_from_graph

    return BuildIndex.from_rows(index_rows_from_graph(mini_graph))


def _git(cwd: Path, *args: str, author: tuple[str, str], date: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": author[0], "GIT_AUTHOR_EMAIL": author[1],
           "GIT_COMMITTER_NAME": author[0], "GIT_COMMITTER_EMAIL": author[1],
           "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout


@pytest.fixture(scope="session")
def history_repo(tmp_path_factory) -> Path:
    """The fixture repo as a git repo with known history:

    1. Ada  "Initial import"                         (everything)
    2. Bob  "Tweak helper (#12)"                      squash-merged PR 12, modifies utils.helper
    3. Cy   "Speed up turbo" + "Fixes #3" (branch)    modifies core.TurboEngine.run
    4. Ada  "Merge pull request #15 from cy/feature"  merge commit: PR 15 includes commit 3
    """
    repo = tmp_path_factory.mktemp("history") / "mini"
    shutil.copytree(MINI_REPO, repo)
    ada, bob, cy = ("Ada", "ada@example.com"), ("Bob", "bob@example.com"), ("Cy", "cy@example.com")
    _git(repo, "init", "-q", "-b", "main", author=ada, date="2024-01-01T10:00:00+00:00")
    _git(repo, "add", ".", author=ada, date="2024-01-01T10:00:00+00:00")
    _git(repo, "commit", "-q", "-m", "Initial import", author=ada, date="2024-01-01T10:00:00+00:00")
    utils = repo / "src/minipkg/utils.py"
    utils.write_text(utils.read_text().replace("return n + 1", "return n + 1  # add one"))
    _git(repo, "commit", "-q", "-am", "Tweak helper (#12)", author=bob, date="2024-02-01T10:00:00+00:00")
    _git(repo, "checkout", "-q", "-b", "feature", author=cy, date="2024-03-01T10:00:00+00:00")
    core = repo / "src/minipkg/core.py"
    core.write_text(core.read_text().replace("return super().run(x) * 2", "return super().run(x) * 2  # turbo"))
    _git(repo, "commit", "-q", "-am", "Speed up turbo\n\nFixes #3", author=cy, date="2024-03-01T10:00:00+00:00")
    _git(repo, "checkout", "-q", "main", author=ada, date="2024-03-02T10:00:00+00:00")
    _git(repo, "merge", "-q", "--no-ff", "feature", "-m", "Merge pull request #15 from cy/feature", author=ada,
         date="2024-03-02T10:00:00+00:00")
    return repo


@pytest.fixture(scope="session")
def store():
    """A Neo4j store, or skip when no database is reachable (set NEO4J_URI etc. to run these tests)."""
    from app.graph.store import GraphStore

    s = GraphStore(os.environ.get("NEO4J_URI", "bolt://localhost:7687"), os.environ.get("NEO4J_USER", "neo4j"),
                   os.environ.get("NEO4J_PASSWORD", "verigraph-password"))
    try:
        s.verify()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Neo4j not reachable: {exc}")
    s.ensure_schema()
    yield s
    s.close()
