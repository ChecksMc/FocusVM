import os
from pathlib import Path
from unittest.mock import patch

import pytest

from focusvm.local_state import (
    PrivateFileError,
    atomic_write,
    check_private_directory,
    file_lock,
    open_owned_file,
    open_private_file,
)


def test_private_state_keeps_modes_and_accepts_user_readable_legacy_config(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "focusvm" / "state.json"
    with file_lock(destination):
        assert destination.parent.stat().st_mode & 0o777 == 0o700
        assert destination.with_name(".state.json.lock").stat().st_mode & 0o777 == 0o600
        atomic_write(destination, "initial")
    assert destination.stat().st_mode & 0o777 == 0o600

    destination.chmod(0o644)
    with open(destination, encoding="utf-8", opener=open_private_file) as stream:
        assert stream.read() == "initial"


def test_private_read_rejects_an_unsafe_parent(tmp_path: Path) -> None:
    directory = tmp_path / "focusvm"
    directory.mkdir()
    destination = directory / "state.json"
    destination.write_text("example")
    directory.chmod(0o775)

    with pytest.raises(PrivateFileError, match="safe to write"):
        check_private_directory(directory)
    with pytest.raises(PrivateFileError, match="safe to write"):
        open_private_file(destination, os.O_RDONLY)

    alias = tmp_path / "alias"
    alias.symlink_to(directory, target_is_directory=True)
    with pytest.raises(PrivateFileError, match="safe to write"):
        open_private_file(alias / destination.name, os.O_RDONLY)

    with open(destination, encoding="utf-8", opener=open_owned_file) as stream:
        assert stream.read() == "example"


def test_private_lock_and_truncation_reject_hardlinks(tmp_path: Path) -> None:
    target = tmp_path / "owned"
    target.write_text("preserve")
    lock_path = tmp_path / ".state.json.lock"
    lock_path.hardlink_to(target)

    with pytest.raises(PrivateFileError, match="hard links"):
        with file_lock(tmp_path / "state.json"):
            pytest.fail("Opened an unsafe lock")
    with pytest.raises(PrivateFileError, match="hard links"):
        open_private_file(lock_path, os.O_WRONLY | os.O_TRUNC)
    assert target.read_text() == "preserve"


def test_private_read_rejects_group_writable_file(tmp_path: Path) -> None:
    destination = tmp_path / "state.json"
    destination.write_text("example")
    destination.chmod(0o664)
    with pytest.raises(PrivateFileError, match="other writers"):
        open_private_file(destination, os.O_RDONLY)


def test_atomic_write_does_not_follow_parent_replacement(tmp_path: Path) -> None:
    directory = tmp_path / "focusvm"
    directory.mkdir()
    renamed = tmp_path / "moved"
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    (unrelated / "state.json").write_text("keep")
    fsync = os.fsync
    moved = False

    def rename_before_publish(descriptor: int) -> None:
        nonlocal moved
        if not moved:
            directory.rename(renamed)
            directory.symlink_to(unrelated, target_is_directory=True)
            moved = True
        fsync(descriptor)

    with patch("focusvm.local_state.os.fsync", side_effect=rename_before_publish):
        atomic_write(directory / "state.json", "published")
    assert (renamed / "state.json").read_text() == "published"
    assert list(renamed.iterdir()) == [renamed / "state.json"]
    assert (unrelated / "state.json").read_text() == "keep"
