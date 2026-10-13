"""PR reports analyze immutable base snapshots without executing target code."""

import subprocess

from app.analysis.pr_review import review
from app.config import Settings
from app.ingest.limits import check_checkout_limits


def test_review_uses_merge_base_with_source_links(history_repo):
    report = review(history_repo, "main^1", "feature", "example/project")
    assert report["base_sha"] != report["head_sha"]
    assert report["llm_used"] is False
    assert any(row["qualified_name"] == "minipkg.core.TurboEngine.run" for row in report["changed"])
    assert report["evidence"]
    assert all(f"/blob/{report['base_sha']}/" in row["url"] for row in report["evidence"])
    assert "No LLM or tests were run" in report["markdown"]
    assert "unverifiable" in report["markdown"]


def test_review_reports_new_files_and_does_not_run_them(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        return subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.com",
                               *args], check=True, capture_output=True, text=True).stdout.strip()

    git("init", "-q")
    marker = tmp_path / "executed"
    (repo / "unsafe.py").write_text(f"open({str(marker)!r}, 'w').write('executed')\ndef base():\n    return 1\n")
    git("add", ".")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    (repo / "added.ts").write_text("export function added() { return 1; }\n")
    git("add", ".")
    git("commit", "-qm", "new file")
    report = review(repo, base, "HEAD")
    assert report["added_files"] == ["added.ts"]
    assert "added.ts" in report["markdown"] and "impact unverifiable" in report["markdown"]
    assert not marker.exists()


def test_checkout_file_and_byte_limits(tmp_path):
    import pytest

    (tmp_path / "first.py").write_text("12345")
    settings = Settings(_env_file=None, max_repo_files=1, max_repo_bytes=5)
    check_checkout_limits(tmp_path, settings)
    (tmp_path / "second.ts").write_text("1")
    with pytest.raises(ValueError, match="Repository exceeds"):
        check_checkout_limits(tmp_path, settings)
    (tmp_path / "second.ts").unlink()
    settings.max_repo_bytes = 4
    with pytest.raises(ValueError, match="Repository exceeds"):
        check_checkout_limits(tmp_path, settings)
