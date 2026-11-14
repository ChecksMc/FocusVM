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
        "focusvm.cli.diagnose_configured",
        return_value=[Diagnostic("libvirt", "error", "Unavailable")],
    ) as diagnose:
        assert main(["doctor", "--json"]) == 3
    assert diagnose.call_args.args[0].config_dir == tmp_path / "focusvm"
    assert json.loads(capsys.readouterr().out) == [
        {"name": "libvirt", "status": "error", "detail": "Unavailable"}
    ]


@pytest.mark.parametrize(
    ("pools", "expected_pool"),
    [
        (("default", "focusvm"), "focusvm"),
        (("default", "spare"), "default"),
        (("spare",), "spare"),
    ],
)
def test_setup_chooses_preferred_pool_and_retains_saved_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    pools: tuple[str, ...],
    expected_pool: str,
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = tmp_path / "focusvm" / "config.toml"
    checks = [Diagnostic("storage_pool", "ok", "Available")]
    with (
        patch("focusvm.setup.setup_pools", return_value=pools) as discover,
        patch("focusvm.setup.diagnose", return_value=checks) as diagnose,
    ):
        assert main(["setup", "--json"]) == 0
        created = json.loads(capsys.readouterr().out)
        assert created == {
            "path": str(path),
            "storage_pool": expected_pool,
            "created": True,
            "checks": [{"name": "storage_pool", "status": "ok", "detail": "Available"}],
            "ready": True,
        }
        assert diagnose.call_args.args[1] == expected_pool
        store = ConfigStore(path)
        store.update(lambda current: replace(current, vm_defaults=VMDefaults(cpus=8)))
        original = path.read_bytes()
        assert main(["setup", "--json"]) == 0
        assert json.loads(capsys.readouterr().out) == {**created, "created": False}
        assert main(["setup", "--storage-pool", "different"]) == 2
        assert "already configured" in capsys.readouterr().err
    discover.assert_called_once_with()
    assert path.read_bytes() == original


@pytest.mark.parametrize(
    ("pools", "message"),
    [
        ((), "No active directory storage pool has free space"),
        (("sda", "sdb"), "Available pools are 'sda', 'sdb'"),
    ],
)
def test_setup_reports_missing_or_ambiguous_pools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    pools: tuple[str, ...],
    message: str,
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    with patch("focusvm.setup.setup_pools", return_value=pools):
        assert main(["setup"]) == 3
        assert message in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


def test_setup_keeps_failed_host_checks_and_avoids_writing_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    checks = [
        Diagnostic("storage_pool", "ok", "Available"),
        Diagnostic("qemu-img", "error", "Install qemu-img"),
    ]
    with (
        patch("focusvm.setup.setup_pools") as discover,
        patch("focusvm.setup.diagnose", return_value=checks) as diagnose,
    ):
        assert main(["setup", "--storage-pool", "spare", "--json"]) == 3
    discover.assert_not_called()
    assert diagnose.call_args.args[1] == "spare"
    payload = json.loads(capsys.readouterr().out)
    assert not payload["created"] and not payload["ready"]
    assert payload["checks"][1]["detail"] == "Install qemu-img"
    assert list(tmp_path.iterdir()) == []


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
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
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
    start.assert_called_once_with(
        vm, PROFILE_ID, None, duration_minutes=None, cage=False
    )
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
    start.assert_called_once_with(
        vm, PROFILE_ID, "strict", duration_minutes=None, cage=False
    )
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
    start.assert_called_once_with(
        None, PROFILE_ID, "flexible", duration_minutes=None, cage=False
    )
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
        {"id": "deep-work", "name": "Deep Work", "network": "balanced"},
        {"id": "contest", "name": "Contest", "network": "strict"},
        {"id": "research", "name": "Research", "network": "balanced"},
    ]
    assert not session_paths.config_file.exists()


def test_resource_library_cli_uses_data_home(
    session_paths: ConfigPaths, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    reference = tmp_path / "CPP-reference.md"
    reference.write_text("# Reference", encoding="utf-8")
    assert main(["resources", "add", str(reference), "--json"]) == 0
    [entry] = json.loads(capsys.readouterr().out)
    assert entry["name"] == reference.name
    assert entry["kind"] == "markdown"
    assert (
        session_paths.data_dir / "resources" / entry["id"] / reference.name
    ).is_file()
    assert main(["resources", "search", "cpp", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == [entry]
    assert main(["resources", "remove", entry["id"]]) == 0
    assert reference.is_file()
    assert not session_paths.config_file.exists()


@pytest.mark.parametrize(
    ("arguments", "operation"),
    [
        (
            ["sync", "fetch", "--repo", ".", "--remote", "origin", "--branch", "main"],
            "sync_remote",
        ),
        (["tracking", "sync", "--prompt-key"], "_hidden_input"),
        (
            ["research", "save", "https://example.org", "--output", "reference.md"],
            "Research.save",
        ),
        (["packages", "attach", "example.whl"], "build_wheel_media"),
        (["resources", "attach", "example"], "ResourceLibrary.build_iso"),
        (
            [
                "sync",
                "export-guest",
                "--output",
                "guest.bundle",
                "/home/focusvm/test.bundle",
            ],
            "VMService.offline_disk",
        ),
    ],
)
def test_maintenance_cli_refuses_side_effects_during_focus(
    session_paths: ConfigPaths,
    arguments: list[str],
    operation: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with (
        patch("focusvm.cli.SessionService._load", return_value=object()),
        patch(f"focusvm.cli.{operation}") as action,
    ):
        assert main(arguments) == 2
    action.assert_not_called()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "End the focus session" in captured.err


def test_journal_cli_records_and_summarizes_a_project(
    session_paths: ConfigPaths, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        main(
            [
                "journal",
                "record",
                "--project",
                "practice",
                "--minutes",
                "30",
                "--solved",
                "Heap bug",
                "--json",
            ]
        )
        == 0
    )
    [entry] = json.loads(capsys.readouterr().out)
    assert entry["solved"] == ["Heap bug"]
    assert main(["journal", "stats", "--project", "practice", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["duration_minutes"] == 30
    assert main(["journal", "remove", entry["id"], "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == [entry]
    assert not session_paths.config_file.exists()
