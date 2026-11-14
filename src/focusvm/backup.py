"""Export a standalone copy of a stopped VM's disk."""

import os
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

from .virtualization import VirtualizationUnavailable


class BackupError(RuntimeError):
    """The VM disk copy could not be verified or published."""


def backup_disk(disk: Path, output: Path) -> Path:
    output = output.absolute()
    if output.suffix.lower() != ".qcow2":
        raise BackupError("Use a new filename ending in .qcow2 for the disk copy")
    if os.path.lexists(output):
        raise BackupError(f"Backup output already exists: {output}")
    if not os.access(disk, os.R_OK):
        raise BackupError(
            "The host account needs read access to the VM overlay and its base image"
        )
    tool = shutil.which("qemu-img")
    if tool is None:
        raise VirtualizationUnavailable("Install qemu-img to back up the guest disk")

    with TemporaryDirectory(prefix=".focusvm-backup-", dir=output.parent) as directory:
        staged = Path(directory) / "disk.qcow2"
        for operation, arguments in (
            ("copy", ("convert", "-f", "qcow2", "-O", "qcow2", str(disk), str(staged))),
            ("check", ("check", "-q", "-f", "qcow2", str(staged))),
        ):
            completed = subprocess.run(  # noqa: S603 - Installed tool and fixed operations.
                [tool, *arguments],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            if completed.returncode:
                detail = completed.stderr.strip()
                if not detail:
                    detail = f"exit {completed.returncode}"
                raise BackupError(f"qemu-img could not {operation} the disk: {detail}")

        staged.chmod(0o600)
        with staged.open("rb") as stream:
            os.fsync(stream.fileno())
        try:
            os.link(staged, output)
        except FileExistsError as error:
            raise BackupError(f"Backup output already exists: {output}") from error
        parent = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    return output
