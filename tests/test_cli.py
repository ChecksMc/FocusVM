import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest

from focusvm import __version__
from focusvm.cli import main
from focusvm.config import ConfigPaths, ConfigStore, Settings, VMDefaults
from focusvm.diagnostics import Diagnostic
from focusvm.provisioning import ImageInput
from focusvm.sessions import DEFAULT_NETWORK, PROFILE_ID, FocusSession, SessionError
from focusvm.viewer import ViewerError
from focusvm.virtualization import VirtualizationUnavailable, VMStatus


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


def test_doctor_json_reports_missing_prerequisites(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    with patch(
        "focusvm.cli.diagnose",
        return_value=[Diagnostic("libvirt", "error", "Unavailable")],
    ) as diagnose:
        assert main(["doctor", "--json"]) == 3
    assert diagnose.call_args.args[1] is None
    assert json.loads(capsys.readouterr().out) == [
        {"name": "libvirt", "status": "error", "detail": "Unavailable"}
    ]


def test_create_uses_config_defaults_and_prompts_for_password(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    assert main(["init", "--storage-pool", "work"]) == 0
    capsys.readouterr()
    status = VMStatus(uuid4(), "work", "shut-off", False, 3, 4096, 2**35, 2**18)
    args = [
        "create",
        "--name",
        "work",
        "--image",
        "base.qcow2",
        "--manifest",
        "manifest.json",
        "--signature",
        "manifest.sig",
        "--keyring",
        "publisher.gpg",
        "--cpus",
        "3",
        "--json",
    ]
    with (
        patch("focusvm.cli.getpass", side_effect=["guest secret", "guest secret"]),
        patch("focusvm.cli.VMService.create", return_value=status) as create,
    ):
        assert main(args) == 0
    create.assert_called_once_with(
        "work",
        ImageInput(
            Path("base.qcow2"),
            Path("manifest.json"),
            Path("manifest.sig"),
            Path("publisher.gpg"),
        ),
        "guest secret",
        "work",
        VMDefaults(cpus=3),
    )
    captured = capsys.readouterr()
    assert "guest secret" not in captured.out + captured.err
    assert json.loads(captured.out)["uuid"] == str(status.uuid)


@pytest.fixture
def session_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ConfigPaths:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    return ConfigPaths.from_environment()


def test_start_session_uses_configured_vm_and_prints_public_json(
    session_paths: ConfigPaths, capsys: pytest.CaptureFixture[str]
) -> None:
    vm = uuid4()
    settings = Settings.for_pool("work")
    ConfigStore(session_paths.config_file).initialize(
        replace(settings, application=replace(settings.application, default_vm=vm))
    )
    started_at = "2026-11-14T20:00:00+00:00"
    session = FocusSession(
        vm=vm,
        vm_name="work",
        profile=PROFILE_ID,
        network=DEFAULT_NETWORK,
        state="active",
        started_at=started_at,
        detail=None,
    )
    with patch("focusvm.cli.SessionService.start", return_value=session) as start:
        assert main(["session", "start", "--json"]) == 0
    start.assert_called_once_with(vm, PROFILE_ID, DEFAULT_NETWORK)
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out) == {
        "vm": str(vm),
        "vm_name": "work",
        "profile": "deep-work",
        "network": "balanced",
        "state": "active",
        "started_at": started_at,
        "detail": None,
    }


def test_start_session_uses_explicit_vm_and_prints_controls(
    session_paths: ConfigPaths, capsys: pytest.CaptureFixture[str]
) -> None:
    vm = uuid4()
    settings = Settings.for_pool("work")
    ConfigStore(session_paths.config_file).initialize(
        replace(settings, application=replace(settings.application, default_vm=uuid4()))
    )
    session = FocusSession(
        vm=vm,
        vm_name="work",
        profile=PROFILE_ID,
        network="strict",
        state="active",
        started_at="2026-11-14T20:00:00+00:00",
        detail=None,
    )
    with patch("focusvm.cli.SessionService.start", return_value=session) as start:
        assert main(["session", "start", "--vm", str(vm), "--network", "strict"]) == 0
    start.assert_called_once_with(vm, PROFILE_ID, "strict")
    out = capsys.readouterr().out
    assert "Shift+F11" in out and "Shift+F12" in out
    assert "focusvm session end" in out
    assert "is active" in out


def test_session_inspection_and_end_do_not_require_valid_config(
    session_paths: ConfigPaths, capsys: pytest.CaptureFixture[str]
) -> None:
    session_paths.config_dir.mkdir(parents=True)
    session_paths.config_file.write_text("invalid settings", encoding="utf-8")
    with patch("focusvm.cli.SessionService.status", return_value=None) as status:
        assert main(["session", "status", "--json"]) == 0
    status.assert_called_once_with()
    assert json.loads(capsys.readouterr().out) is None

    with patch("focusvm.cli.SessionService.end") as end:
        assert main(["session", "end", "--json"]) == 0
    end.assert_called_once_with()
    assert json.loads(capsys.readouterr().out) == {"state": "ended"}


def test_session_unavailable_and_reopen_failure_keep_stdout_clean(
    session_paths: ConfigPaths, capsys: pytest.CaptureFixture[str]
) -> None:
    with patch(
        "focusvm.cli.SessionService.start",
        side_effect=VirtualizationUnavailable("Flexible networking is unavailable"),
    ) as start:
        assert main(["session", "start", "--network", "flexible", "--json"]) == 3
    start.assert_called_once_with(None, PROFILE_ID, "flexible")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Flexible networking is unavailable" in captured.err

    with patch(
        "focusvm.cli.SessionService.reopen",
        side_effect=SessionError("The focus session could not be verified"),
    ) as reopen:
        assert main(["session", "reopen", "--json"]) == 4
    reopen.assert_called_once_with()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "could not be verified" in captured.err

    with patch(
        "focusvm.cli.SessionService.reopen", side_effect=ViewerError("Viewer exited")
    ):
        assert main(["session", "reopen", "--json"]) == 4
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Viewer exited" in captured.err


def test_profiles_lists_only_implemented_profiles(
    session_paths: ConfigPaths, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["profiles", "list", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == [
        {"id": "deep-work", "name": "Deep Work", "network": "balanced"}
    ]
    assert not session_paths.config_file.exists()
