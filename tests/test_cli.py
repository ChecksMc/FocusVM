import os
import subprocess
import sys
from pathlib import Path

from focusvm import __version__


def run_focusvm(config_home: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "focusvm", *arguments],
        env={**os.environ, "XDG_CONFIG_HOME": str(config_home)},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def test_help_and_version(tmp_path: Path) -> None:
    help_result = run_focusvm(tmp_path, "--help")
    assert help_result.returncode == 0
    assert "init" in help_result.stdout
    assert "config" in help_result.stdout

    version_result = run_focusvm(tmp_path, "--version")
    assert version_result.returncode == 0
    assert version_result.stdout.strip() == f"focusvm {__version__}"
    assert list(tmp_path.iterdir()) == []


def test_init_and_validate(tmp_path: Path) -> None:
    path = tmp_path / "focusvm" / "config.toml"
    result = run_focusvm(tmp_path, "init", "--storage-pool", "work")
    assert result.returncode == 0
    assert str(path) in result.stdout
    original = path.read_bytes()

    result = run_focusvm(tmp_path, "config", "validate")
    assert result.returncode == 0
    assert str(path) in result.stdout

    result = run_focusvm(tmp_path, "init", "--storage-pool", "work")
    assert result.returncode == 0
    assert "already initialized" in result.stdout
    assert path.read_bytes() == original

    result = run_focusvm(tmp_path, "init", "--storage-pool", "another")
    assert result.returncode == 2
    assert result.stderr
    assert path.read_bytes() == original


def test_validation_reports_missing_and_malformed_configuration(tmp_path: Path) -> None:
    result = run_focusvm(tmp_path, "config", "validate")
    assert result.returncode == 2
    assert "focusvm init --storage-pool NAME" in result.stderr

    path = tmp_path / "focusvm" / "config.toml"
    path.parent.mkdir()
    path.write_text("schema_version = [", encoding="utf-8")

    result = run_focusvm(tmp_path, "config", "validate")
    assert result.returncode == 2
    assert result.stderr


def test_invalid_pool_is_rejected_without_writing_configuration(tmp_path: Path) -> None:
    result = run_focusvm(tmp_path, "init", "--storage-pool", "invalid/name")
    assert result.returncode == 2
    assert "storage_pool" in result.stderr
    assert not (tmp_path / "focusvm" / "config.toml").exists()


def test_config_directory_failure_is_reported_as_operation_failure(
    tmp_path: Path,
) -> None:
    (tmp_path / "focusvm").touch()

    result = run_focusvm(tmp_path, "init", "--storage-pool", "work")
    assert result.returncode == 4
    assert "focusvm" in result.stderr
    assert "Traceback" not in result.stderr
