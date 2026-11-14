import os
import shutil
import subprocess
from pathlib import Path

import pytest

from focusvm.git_sync import (
    GitSyncError,
    GitSyncInputError,
    create_bundle,
    import_bundle,
    sync_remote,
)

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="Git is unavailable"
)


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],  # noqa: S607 - Git is a test prerequisite resolved from PATH.
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout.strip()


def init_repo(path: Path) -> Path:
    path.mkdir()
    git(path, "init", "--initial-branch=main")
    git(path, "config", "user.name", "FocusVM tests")
    git(path, "config", "user.email", "test@example.invalid")
    return path


def commit(repo: Path, content: str) -> str:
    (repo / "source.txt").write_text(content, encoding="utf-8")
    git(repo, "add", "source.txt")
    git(repo, "-c", "commit.gpgSign=false", "commit", "-m", content)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture(autouse=True)
def isolated_git_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def test_bundle_round_trip_keeps_destination_branch_and_worktree(
    tmp_path: Path,
) -> None:
    source = init_repo(tmp_path / "source")
    original = commit(source, "original")
    git(source, "branch", "work/topic")
    output = tmp_path / "transfer.bundle"
    create_bundle(source, "work/topic", output)
    assert output.stat().st_mode & 0o777 == 0o600

    target = init_repo(tmp_path / "target")
    target_original = commit(target, "local work")
    (target / "source.txt").write_text("unstaged local edit", encoding="utf-8")
    (target / "staged.txt").write_text("staged local edit", encoding="utf-8")
    git(target, "add", "staged.txt")
    before = git(target, "status", "--porcelain")

    ref = import_bundle(target, output, "work/topic")
    assert ref == "refs/focusvm/bundles/work/topic"
    assert git(target, "rev-parse", ref) == original
    assert import_bundle(target, output, "work/topic") == ref
    assert git(target, "rev-parse", "HEAD") == target_original
    assert git(target, "status", "--porcelain") == before

    git(source, "switch", "work/topic")
    commit(source, "new work")
    next_output = tmp_path / "new.bundle"
    create_bundle(source, "work/topic", next_output)
    with pytest.raises(GitSyncInputError, match="already holds a different commit"):
        import_bundle(target, next_output, "work/topic")
    assert git(target, "rev-parse", ref) == original


def test_bundle_rejects_invalid_inputs_and_existing_outputs(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    commit(repo, "original")
    output = tmp_path / "existing.bundle"
    output.write_bytes(b"preserve this")
    with pytest.raises(GitSyncInputError, match="already exists"):
        create_bundle(repo, "main", output)
    assert output.read_bytes() == b"preserve this"

    link = tmp_path / "linked.bundle"
    link.symlink_to(tmp_path / "missing.bundle")
    with pytest.raises(GitSyncInputError, match="already exists"):
        create_bundle(repo, "main", link)
    with pytest.raises(GitSyncInputError, match="Cannot read"):
        import_bundle(repo, link, "main")
    with pytest.raises(GitSyncInputError, match="Invalid Git bundle"):
        import_bundle(repo, output, "main")

    with pytest.raises(GitSyncInputError, match="Invalid Git branch name"):
        create_bundle(repo, "main:other", tmp_path / "new.bundle")
    subdirectory = repo / "src"
    subdirectory.mkdir()
    with pytest.raises(GitSyncInputError, match="root directory"):
        create_bundle(subdirectory, "main", tmp_path / "new.bundle")


def test_fetch_only_updates_review_ref_and_preserves_divergent_history(
    tmp_path: Path,
) -> None:
    remote = init_repo(tmp_path / "remote")
    original = commit(remote, "original")
    repo = init_repo(tmp_path / "repo")
    local = commit(repo, "local work")
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "config", "remote.origin.fetch", "+refs/heads/*:refs/heads/*")
    git(repo, "config", "fetch.prune", "true")
    git(remote, "tag", "remote-tag")

    ref = sync_remote(repo, "origin", "main", "fetch")
    assert ref == "refs/focusvm/remotes/origin/main"
    assert git(repo, "rev-parse", ref) == original
    assert git(repo, "rev-parse", "HEAD") == local
    assert git(repo, "tag", "--list") == ""
    assert not (repo / ".git" / "FETCH_HEAD").exists()

    next_tip = commit(remote, "updated work")
    assert sync_remote(repo, "origin", "main", "fetch") == ref
    assert git(repo, "rev-parse", ref) == next_tip
    git(remote, "reset", "--hard", original)
    commit(remote, "divergent work")
    with pytest.raises(GitSyncError, match="diverged"):
        sync_remote(repo, "origin", "main", "fetch")
    assert git(repo, "rev-parse", ref) == next_tip
    assert git(repo, "for-each-ref", "refs/focusvm/staging") == ""


def test_push_only_sends_selected_branch_and_refuses_non_fast_forward(
    tmp_path: Path,
) -> None:
    remote = tmp_path / "remote.git"
    remote.mkdir()
    git(remote, "init", "--bare", "--initial-branch=main")
    repo = init_repo(tmp_path / "repo")
    original = commit(repo, "original")
    git(repo, "branch", "do-not-send")
    git(repo, "-c", "tag.gpgSign=false", "tag", "-a", "remote-tag", "-m", "tag")
    git(repo, "config", "push.followTags", "true")
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "config", "remote.origin.mirror", "true")

    assert sync_remote(repo, "origin", "main", "push") == "refs/heads/main"
    assert git(remote, "for-each-ref", "--format=%(refname)") == "refs/heads/main"
    assert git(remote, "rev-parse", "main") == original
    next_tip = commit(repo, "second")
    assert sync_remote(repo, "origin", "main", "push") == "refs/heads/main"
    assert git(remote, "rev-parse", "main") == next_tip

    git(repo, "reset", "--hard", original)
    commit(repo, "divergent work")
    with pytest.raises(GitSyncError, match="fast-forward"):
        sync_remote(repo, "origin", "main", "push")
    assert git(remote, "rev-parse", "main") == next_tip


def test_remote_rejects_helpers_and_unconfigured_names(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    with pytest.raises(GitSyncInputError, match="Invalid configured Git remote"):
        sync_remote(repo, "-origin", "main", "fetch")
    with pytest.raises(GitSyncInputError, match="No URL"):
        sync_remote(repo, "origin", "main", "fetch")

    git(repo, "config", "remote.origin.url", "https://example.invalid/example.git")
    git(repo, "config", "url.ext::false.insteadOf", "https://example.invalid/")
    with pytest.raises(GitSyncInputError, match="Unsupported Git remote URL"):
        sync_remote(repo, "origin", "main", "fetch")

    git(repo, "config", "--add", "remote.origin.url", os.fspath(tmp_path / "other"))
    with pytest.raises(GitSyncInputError, match="one valid url"):
        sync_remote(repo, "origin", "main", "fetch")
