from pathlib import Path

from app.graph.builder import build_structure
from app.graph.parser import PythonParser
from app.ingest.clone import checkout_repo, github_slug, repo_name
from app.ingest.history import changed_functions, mine_history, parse_fixes


def test_mine_history_links_commits_prs_and_functions(history_repo, tmp_path):
    checkout = checkout_repo(str(history_repo), None, tmp_path / "co", tmp_path / "mirrors")
    assert checkout.has_history and len(checkout.commit_sha) == 40
    g = build_structure(checkout.path, "mini", str(history_repo), checkout.commit_sha, ["import", "name"])
    stats = mine_history(checkout.path, g, 100)
    assert stats["commits"] == 4
    by_title = {c.title: c for c in g.commits.values()}
    assert by_title["Merge pull request #15 from cy/feature"].order == 0
    assert by_title["Initial import"].order == 3
    tweak, turbo = by_title["Tweak helper (#12)"], by_title["Speed up turbo"]
    assert g.pull_requests[12].commits == {tweak.sha}
    assert turbo.sha in g.pull_requests[15].commits  # branch commit of a merge-commit PR
    assert g.pull_requests[15].fixes == {3}  # "Fixes #3" came from the branch commit
    assert g.modifies[(tweak.sha, "minipkg.utils.helper")] == {"lines_added": 1, "lines_deleted": 1,
                                                               "change_type": "MODIFY"}
    assert (turbo.sha, "minipkg.core.TurboEngine.run") in g.modifies
    assert not any(sha == turbo.sha and fn != "minipkg.core.TurboEngine.run" for sha, fn in g.modifies)


def test_checkout_pins_a_commit(history_repo, tmp_path):
    first = checkout_repo(str(history_repo), "HEAD~1", tmp_path / "a", tmp_path / "m")
    assert "# add one" in (first.path / "src/minipkg/utils.py").read_text()
    assert "# turbo" not in (first.path / "src/minipkg/core.py").read_text()


def test_fast_checkout_fetches_pinned_snapshot_without_full_history(history_repo, tmp_path):
    from git import Repo as GitRepo

    base = GitRepo(history_repo).commit("HEAD~1").hexsha
    checkout = checkout_repo(f"file://{history_repo}", base, tmp_path / "snapshot", tmp_path / "m", history=False)
    assert checkout.commit_sha == base
    assert not checkout.has_history
    assert len(list(GitRepo(checkout.path).iter_commits())) == 1
    assert (checkout.path / ".git" / "shallow").exists()
    assert not (tmp_path / "m").exists()


def test_builds_of_one_repository_take_turns_on_its_mirror(history_repo, tmp_path):
    """A second build of a repository waits for the first, instead of updating a mirror that is still being
    written (or that the first deletes after a failed clone)."""
    import threading

    from app.ingest.clone import _mirror_lock

    mirrors = tmp_path / "mirrors"
    mirrors.mkdir()
    done = []
    with _mirror_lock(mirrors / "mini.git"):  # another build is fetching this repository
        build = threading.Thread(target=lambda: done.append(
            checkout_repo(f"file://{history_repo}", None, tmp_path / "co", mirrors)))
        build.start()
        build.join(0.5)
        assert build.is_alive() and not (mirrors / "mini.git").exists()  # waiting, not cloning
    build.join(30)
    assert done and done[0].has_history and len(done[0].commit_sha) == 40


def test_a_failed_mirror_clone_is_retried_over_http11(history_repo, tmp_path, monkeypatch):
    from git import GitCommandError
    from git import Repo as GitRepo

    from app.ingest import clone

    real = GitRepo.clone_from
    envs = []

    def flaky(url, to_path, **kwargs):
        if kwargs.get("mirror"):
            envs.append(kwargs.get("env"))
            if len(envs) == 1:
                Path(to_path).mkdir()  # the network dropped mid-transfer and left a partial mirror
                raise GitCommandError(["git", "clone"], 128, "curl 92 HTTP/2 stream 5 was not closed cleanly")
        return real(url, to_path, **kwargs)
    monkeypatch.setattr(clone.GitRepo, "clone_from", flaky)
    monkeypatch.setattr(clone, "RETRY_WAIT_S", 0)
    checkout = clone.checkout_repo(f"file://{history_repo}", None, tmp_path / "co", tmp_path / "mirrors")
    assert checkout.has_history and len(checkout.commit_sha) == 40
    assert envs == [None, clone._HTTP11]  # the retry used HTTP/1.1


def test_changed_functions_uses_both_sides_of_the_diff():
    before = "def a():\n    return 1\n\n\ndef b():\n    return 2\n"
    after = "def a():\n    return 1\n\n\ndef b():\n    x = 2\n    return x\n"
    changed = changed_functions(PythonParser(), "m", before, after, added=[6, 7], deleted=[6])
    assert changed == {"m.b": [2, 1]}


def test_fix_references_and_slugs():
    assert parse_fixes("Fixes #12, closes #7. Resolved: psf/requests#99; refs #5") == {12, 7, 99}
    assert github_slug("https://github.com/psf/requests.git") == "psf/requests"
    assert github_slug("git@github.com:pallets/click.git") == "pallets/click"
    assert repo_name("/some/path/project/") == "project"


def test_fork_sync_merges_do_not_become_pull_requests(tmp_path):
    """A contributor syncs upstream main into their fork branch through *their fork's* PR #1, and upstream later
    merges that branch as PR #20. "#1" is not a PR of this repository: it must not claim upstream's commits."""
    import shutil

    from conftest import MINI_REPO, _git

    repo = tmp_path / "fork"
    shutil.copytree(MINI_REPO, repo)
    ada, bob, dee = ("Ada", "ada@example.com"), ("Bob", "bob@example.com"), ("Dee", "dee@example.com")
    _git(repo, "init", "-q", "-b", "main", author=ada, date="2024-01-01T10:00:00+00:00")
    _git(repo, "add", ".", author=ada, date="2024-01-01T10:00:00+00:00")
    _git(repo, "commit", "-q", "-m", "Initial import", author=ada, date="2024-01-01T10:00:00+00:00")
    _git(repo, "checkout", "-q", "-b", "feature", author=dee, date="2024-01-02T10:00:00+00:00")
    core = repo / "src/minipkg/core.py"
    core.write_text(core.read_text().replace("return super().run(x) * 2", "return super().run(x) * 2  # turbo"))
    _git(repo, "commit", "-q", "-am", "Improve turbo", author=dee, date="2024-01-02T10:00:00+00:00")
    _git(repo, "checkout", "-q", "main", author=bob, date="2024-02-01T10:00:00+00:00")
    utils = repo / "src/minipkg/utils.py"
    utils.write_text(utils.read_text().replace("return n + 1", "return n + 1  # add one"))
    _git(repo, "commit", "-q", "-am", "Tweak helper (#12)", author=bob, date="2024-02-01T10:00:00+00:00")
    _git(repo, "checkout", "-q", "feature", author=dee, date="2024-02-02T10:00:00+00:00")
    _git(repo, "merge", "-q", "--no-ff", "main", "-m", "Merge pull request #1 from upstream/main", author=dee,
         date="2024-02-02T10:00:00+00:00")
    _git(repo, "checkout", "-q", "main", author=ada, date="2024-03-01T10:00:00+00:00")
    _git(repo, "merge", "-q", "--no-ff", "feature", "-m", "Merge pull request #20 from dee/feature", author=ada,
         date="2024-03-01T10:00:00+00:00")

    checkout = checkout_repo(str(repo), None, tmp_path / "co", tmp_path / "mirrors")
    g = build_structure(checkout.path, "mini", str(repo), checkout.commit_sha, ["import", "name"])
    stats = mine_history(checkout.path, g, 100)
    by_title = {c.title: c for c in g.commits.values()}
    tweak, turbo = by_title["Tweak helper (#12)"], by_title["Improve turbo"]
    assert 1 not in g.pull_requests and stats["fork_merges_skipped"] == 1
    assert {n for n, pr in g.pull_requests.items() if tweak.sha in pr.commits} == {12}
    assert turbo.sha in g.pull_requests[20].commits
