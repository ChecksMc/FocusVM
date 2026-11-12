"""Private files and serialized atomic writes shared by settings and VM state."""

import fcntl
import os
import stat
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile


class PrivateFileError(ValueError):
    """A private FocusVM path is unsafe to use."""


def open_owned_file(path: str | Path, flags: int) -> int:
    descriptor = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise PrivateFileError(f"Expected a regular file owned by you: {path}")
    except (OSError, PrivateFileError):
        os.close(descriptor)
        raise
    return descriptor


@contextmanager
def file_lock(path: Path) -> Generator[None]:
    parent = path.parent
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = parent.lstat()
    if (
        not stat.S_ISDIR(directory.st_mode)
        or directory.st_uid != os.getuid()
        or directory.st_mode & 0o022
    ):
        raise PrivateFileError(
            f"Directory must be owned by you and safe to write: {parent}"
        )

    lock_path = path.with_name(f".{path.name}.lock")
    lock_fd = open_owned_file(lock_path, os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(lock_fd)


def atomic_write(path: Path, content: str) -> None:
    with NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.stem}-",
        delete=False,
    ) as stream:
        staged = Path(stream.name)
        try:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
            os.replace(staged, path)
            parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        finally:
            staged.unlink(missing_ok=True)
