import json
import shutil
import subprocess
from pathlib import Path

import pytest

from focusvm.backup import BackupError, backup_disk


def test_backup_flattens_a_real_qcow2_chain_without_changing_it(tmp_path: Path) -> None:
    tool = shutil.which("qemu-img")
    if tool is None:
        pytest.skip("qemu-img required to check a real QCOW2 backup")
    base = tmp_path / "base.qcow2"
    overlay = tmp_path / "overlay.qcow2"
    output = tmp_path / "backup.qcow2"
    for args in (
        ("create", "-f", "qcow2", str(base), "1M"),
        ("create", "-f", "qcow2", "-F", "qcow2", "-b", str(base), str(overlay)),
    ):
        subprocess.run([tool, *args], check=True, capture_output=True)
    original = base.read_bytes(), overlay.read_bytes()

    assert backup_disk(overlay, output) == output
    assert output.stat().st_mode & 0o777 == 0o600
    info = subprocess.check_output(
        [tool, "info", "--output=json", str(output)], text=True
    )
    assert json.loads(info)["virtual-size"] == 1024**2
    assert "backing-filename" not in json.loads(info)
    subprocess.run(
        [tool, "compare", "-f", "qcow2", "-F", "qcow2", str(overlay), str(output)],
        check=True,
    )
    assert original == (base.read_bytes(), overlay.read_bytes())
    with pytest.raises(BackupError, match="already exists"):
        backup_disk(overlay, output)


def test_failed_disk_copy_leaves_no_output(tmp_path: Path) -> None:
    if shutil.which("qemu-img") is None:
        pytest.skip("qemu-img required")
    corrupt = tmp_path / "corrupt.qcow2"
    corrupt.write_text("not a disk", encoding="utf-8")
    output = tmp_path / "backup.qcow2"
    with pytest.raises(BackupError, match="could not copy"):
        backup_disk(corrupt, output)
    assert set(tmp_path.iterdir()) == {corrupt}
