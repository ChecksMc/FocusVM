"""Private files and serialized atomic writes shared by settings and VM state."""

import fcntl
import os
import stat
from collections.abc import Generator
from contextlib import contextmanager, suppress
from errno import ELOOP, ENOTDIR
from pathlib import Path
from uuid import uuid4


class PrivateFileError(ValueError):
    """A private FocusVM path is unsafe to use."""


def open_owned_file(path: str | Path, flags: int) -> int:
    """Open a user-supplied owned file without following its final link."""
    return _open_file(path, flags, parent=None)


def open_private_file(path: str | Path, flags: int) -> int:
    """Open managed state through its checked parent directory."""
    file = Path(path)
    with _private_directory(file.parent) as parent:
        return _open_file(file, flags, parent=parent)


def _open_file(path: str | Path, flags: int, *, parent: int | None) -> int:
    name = path if parent is None else Path(path).name
    open_flags = (flags & ~os.O_TRUNC) | os.O_NOFOLLOW | os.O_NONBLOCK
    descriptor = os.open(name, open_flags, 0o600, dir_fd=parent)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise PrivateFileError(f"Expected a regular file owned by you: {path}")
        if info.st_nlink != 1:
            raise PrivateFileError(f"File must have no other hard links: {path}")
        if info.st_mode & 0o022:
            raise PrivateFileError(f"File must be protected from other writers: {path}")
        if flags & os.O_TRUNC:
            os.ftruncate(descriptor, 0)
    except (OSError, PrivateFileError):
        os.close(descriptor)
        raise
    return descriptor


def check_private_directory(path: Path) -> None:
    """Verify that an existing state directory is owned and safe to write."""
    with _private_directory(path):
        pass


@contextmanager
def _private_directory(path: Path) -> Generator[int]:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as error:
        if error.errno in (ELOOP, ENOTDIR) and path.is_symlink():
            raise PrivateFileError(
                f"Directory must be owned by you and safe to write: {path}"
            ) from error
        raise
    try:
        directory = os.fstat(descriptor)
        if directory.st_uid != os.getuid() or directory.st_mode & 0o022:
            raise PrivateFileError(
                f"Directory must be owned by you and safe to write: {path}"
            )
        yield descriptor
    finally:
        os.close(descriptor)


@contextmanager
def file_lock(path: Path) -> Generator[None]:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = path.with_name(f".{path.name}.lock")
    with _private_directory(path.parent) as parent:
        lock_fd = _open_file(lock_path, os.O_CREAT | os.O_RDWR, parent=parent)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(lock_fd)


def atomic_write(path: Path, content: str) -> None:
    with _private_directory(path.parent) as parent:
        staged = f".{path.stem}-{uuid4().hex}"
        descriptor = os.open(
            staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(staged, path.name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        finally:
            with suppress(FileNotFoundError):
                os.unlink(staged, dir_fd=parent)
