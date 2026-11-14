from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

import pytest

from focusvm.guest_export import GuestExportError, export_bundle


def test_bundle_export_opens_disk_readonly_and_refuses_overwrite(
    tmp_path: Path,
) -> None:
    disk = tmp_path / "overlay.qcow2"
    disk.touch()
    output = tmp_path / "working.bundle"

    def download(command: list[str], **_kwargs: object) -> CompletedProcess[str]:
        assert command[1:-1] == [
            "--ro",
            "--format=qcow2",
            "-a",
            str(disk),
            "-i",
            "download",
            "/home/focusvm/working.bundle",
        ]
        Path(command[-1]).write_bytes(b"# v3 git bundle\n")
        return CompletedProcess(command, 0)

    with (
        patch("focusvm.guest_export.shutil.which", return_value="/usr/bin/guestfish"),
        patch("focusvm.guest_export.subprocess.run", side_effect=download) as run,
    ):
        assert export_bundle(disk, "/home/focusvm/working.bundle", output) == output
        assert output.stat().st_mode & 0o777 == 0o600
        with pytest.raises(GuestExportError, match="already exists"):
            export_bundle(disk, "/home/focusvm/working.bundle", output)
        run.assert_called_once()


def test_bundle_export_rejects_guest_path_outside_home(tmp_path: Path) -> None:
    with pytest.raises(GuestExportError, match="under /home/focusvm"):
        export_bundle(
            tmp_path / "disk",
            "/home/focusvm/../../private.bundle",
            tmp_path / "out.bundle",
        )
