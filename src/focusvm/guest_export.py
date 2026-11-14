"""Copy an explicitly chosen bundle from a stopped guest using libguestfs."""

import os
import shutil
import subprocess
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory


class GuestExportError(RuntimeError):
    """The file could not be copied from the offline guest."""


def export_bundle(disk: Path, source: str, output: Path) -> Path:
    path = PurePosixPath(source)
    if (
        not path.is_absolute()
        or not path.is_relative_to("/home/focusvm")
        or ".." in path.parts
        or not source.isprintable()
        or path.suffix != ".bundle"
        or output.suffix != ".bundle"
    ):
        raise GuestExportError(
            "Choose a .bundle file under /home/focusvm and a .bundle output path"
        )
    tool = shutil.which("guestfish")
    if tool is None:
        raise GuestExportError(
            "Install guestfish from your distribution's libguestfs tools"
        )
    if not os.access(disk, os.R_OK):
        raise GuestExportError(
            "The host account needs read access to the VM overlay and its base image"
        )
    destination = output.absolute()
    if os.path.lexists(destination):
        raise GuestExportError(f"Output already exists: {destination}")
    with TemporaryDirectory(
        prefix=".focusvm-bundle-", dir=destination.parent
    ) as directory:
        staged = Path(directory) / "export.bundle"
        result = subprocess.run(  # noqa: S603 - Installed guestfish with fixed readonly command.
            [
                tool,
                "--ro",
                "--format=qcow2",
                "-a",
                str(disk),
                "-i",
                "download",
                path.as_posix(),
                str(staged),
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=120,
            check=False,
            text=True,
        )
        if result.returncode or not staged.is_file():
            raise GuestExportError(
                "guestfish could not read the bundle. Check the guest path and host permissions for the disk chain."
            )
        with staged.open("rb") as stream:
            if stream.readline(64).rstrip(b"\n") not in (
                b"# v2 git bundle",
                b"# v3 git bundle",
            ):
                raise GuestExportError(
                    "The exported file does not have a Git bundle header"
                )
            os.fsync(stream.fileno())
        staged.chmod(0o600)
        try:
            os.link(staged, destination)
        except FileExistsError as error:
            raise GuestExportError(f"Output already exists: {destination}") from error
    return destination
