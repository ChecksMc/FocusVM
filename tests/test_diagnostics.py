import platform
import shutil
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import pytest

from focusvm import diagnostics
from focusvm.config import ConfigPaths
from focusvm.diagnostics import (
    Diagnostic,
    diagnose,
    diagnose_configured,
    redact_diagnostics,
)
from focusvm.virtualization import PoolStatus, VirtualizationError


class _Backend:
    def __init__(self, *, available_bytes: int = 4096, kvm: bool = True) -> None:
        self.available_bytes = available_bytes
        self.kvm = kvm

    def check_capabilities(self) -> None:
        if not self.kvm:
            raise VirtualizationError("KVM unavailable to libvirt")

    def pool_status(self, name: str) -> PoolStatus:
        if name != "focusvm":
            raise VirtualizationError(f"No such directory pool: {name}")
        return PoolStatus(name=name, available_bytes=self.available_bytes)


def _patch_backend(monkeypatch: pytest.MonkeyPatch, backend: _Backend) -> None:
    @contextmanager
    def connection(*, read_only: bool = False) -> Generator[_Backend]:
        assert not read_only
        yield backend

    monkeypatch.setattr(diagnostics, "connect", connection)


def _host_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    def which(name: str) -> str:
        return f"/usr/bin/{name}"

    monkeypatch.setattr(platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(shutil, "which", which)
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
    monkeypatch.setattr(diagnostics, "local_display_backend", lambda: "Wayland")


def _run(tmp_path: Path, pool: str | None) -> dict[str, Diagnostic]:
    paths = ConfigPaths.from_environment(environment={}, home=tmp_path)
    return {result.name: result for result in diagnose(paths, pool)}


def test_doctor_reports_readiness_without_creating_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _host_tools(monkeypatch)
    _patch_backend(monkeypatch, _Backend())

    checks = _run(tmp_path, "focusvm")

    assert all(check.status != "error" for check in checks.values())
    assert checks["libvirt"].status == checks["vm_capabilities"].status == "ok"
    assert "qemu:///system" in checks["libvirt"].detail
    assert checks["cache_directory"].status == "ok"
    assert "4,096 bytes" in checks["storage_pool"].detail
    assert "wayland" in checks["desktop"].detail
    assert checks["guest_image"].status == "warning"
    assert list(tmp_path.iterdir()) == []


def test_doctor_keeps_capability_and_pool_errors_separate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _host_tools(monkeypatch)
    _patch_backend(monkeypatch, _Backend(kvm=False))

    checks = _run(tmp_path, "missing")

    assert checks["libvirt"].status == "ok"
    assert checks["vm_capabilities"].detail.startswith("KVM unavailable to libvirt")
    assert "virt-host-validate qemu" in checks["vm_capabilities"].detail
    assert checks["vm_capabilities"].status == checks["storage_pool"].status == "error"
    assert "missing" in checks["storage_pool"].detail
    assert "pool-list --all" in checks["storage_pool"].detail


def test_doctor_reports_blocked_paths_and_optional_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing_command(_name: str) -> None:
        return None

    _host_tools(monkeypatch)
    _patch_backend(monkeypatch, _Backend(available_bytes=0))
    monkeypatch.setattr(platform, "machine", lambda: "aarch64")
    monkeypatch.setattr(shutil, "which", missing_command)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr(diagnostics, "local_display_backend", lambda: None)
    (tmp_path / ".config").touch()
    cache = tmp_path / ".cache"
    cache.mkdir()
    (cache / "focusvm").symlink_to(cache, target_is_directory=True)

    checks = _run(tmp_path, "focusvm")

    assert checks["architecture"].status == "error"
    assert checks["config_directory"].status == "error"
    assert checks["cache_directory"].status == "error"
    assert checks["state_directory"].status == "ok"
    assert checks["storage_pool"].status == "error"
    assert "Free space" in checks["storage_pool"].detail
    assert checks["qemu-img"].status == "error"
    assert "sudo dnf install gnupg2-verify" in checks["gpgv"].detail
    assert checks["virt-viewer"].status == checks["desktop"].status == "warning"


def test_doctor_warns_when_no_pool_is_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _host_tools(monkeypatch)
    _patch_backend(monkeypatch, _Backend())

    checks = _run(tmp_path, None)

    assert checks["storage_pool"].status == "warning"
    assert "focusvm setup" in checks["storage_pool"].detail


def test_doctor_continues_other_checks_after_connection_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_connection(*, read_only: bool = False) -> None:
        assert not read_only
        raise VirtualizationError("Cannot connect to qemu:///system")

    _host_tools(monkeypatch)
    monkeypatch.setattr(diagnostics, "connect", no_connection)

    checks = _run(tmp_path, "focusvm")

    assert checks["libvirt"].status == "error"
    assert "Cannot connect" in checks["libvirt"].detail
    assert "systemctl status virtqemud.socket" in checks["libvirt"].detail
    assert (
        checks["vm_capabilities"].status == checks["storage_pool"].status == "warning"
    )
    assert checks["qemu-img"].status == "ok"


def test_doctor_continues_after_malformed_config_and_checks_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _host_tools(monkeypatch)
    _patch_backend(monkeypatch, _Backend())
    paths = ConfigPaths.from_environment(environment={}, home=tmp_path)
    paths.config_dir.mkdir(parents=True)
    paths.config_file.write_text("[broken", encoding="utf-8")
    paths.data_dir.mkdir(parents=True)
    paths.data_dir.chmod(0o777)
    checks = {result.name: result for result in diagnose_configured(paths)}

    assert checks["configuration"].status == "error"
    assert "Invalid TOML" in checks["configuration"].detail
    assert checks["data_directory"].status == "error"
    assert checks["libvirt"].status == "ok"
    assert checks["storage_pool"].status == "warning"
    assert paths.config_file.read_text() == "[broken"


def test_doctor_reports_stale_display_and_unavailable_pidfd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "virt-viewer"
    executable.touch(mode=0o700)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("WAYLAND_DISPLAY", "missing")
    monkeypatch.delenv("DISPLAY", raising=False)
    _patch_backend(monkeypatch, _Backend())
    checks = _run(tmp_path, "focusvm")
    assert checks["desktop"].status == checks["focus_viewer"].status == "warning"
    assert "local display" in checks["desktop"].detail
    assert "local Wayland or X11" in checks["focus_viewer"].detail

    monkeypatch.setenv("DISPLAY", ":0")

    def x11_socket(path: Path) -> bool:
        return path.name == "X0"

    monkeypatch.setattr(Path, "is_socket", x11_socket)

    def deny_pidfd(_pid: int) -> int:
        raise OSError("denied")

    monkeypatch.setattr("focusvm.viewer.os.pidfd_open", deny_pidfd)
    checks = _run(tmp_path, "focusvm")
    assert checks["desktop"].status == "ok"
    assert "pidfd unavailable" in checks["focus_viewer"].detail


@pytest.mark.parametrize(
    ("message", "hint"),
    [
        ("Cannot connect to qemu:///system: permission denied", "administrator"),
        ("Cannot connect to qemu:///system: Timeout was reached", "desktop session"),
        ("Install the libvirt Python bindings", "python3-libvirt"),
    ],
)
def test_doctor_suggests_next_steps_for_libvirt_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, message: str, hint: str
) -> None:
    def no_connection() -> None:
        raise VirtualizationError(message)

    _host_tools(monkeypatch)
    monkeypatch.setattr(diagnostics, "connect", no_connection)
    assert hint in _run(tmp_path, "focusvm")["libvirt"].detail


def test_shareable_diagnostics_only_use_fixed_details() -> None:
    private = [
        Diagnostic("configuration", "error", "/home/example/private/config.toml"),
        Diagnostic("libvirt", "error", "Backend provided an unexpected raw message"),
        Diagnostic("storage_pool", "ok", "A confidential pool name"),
        Diagnostic("desktop", "ok", "Desktop environment custom values"),
        Diagnostic("untrusted arbitrary name", "warning", "Raw unknown details"),
    ]
    assert redact_diagnostics(private) == [
        Diagnostic("configuration", "error", "FocusVM settings"),
        Diagnostic("libvirt", "error", "System libvirt connection"),
        Diagnostic("storage_pool", "ok", "Directory storage pool readiness"),
        Diagnostic("desktop", "ok", "Local graphical session"),
        Diagnostic("check", "warning", "Host prerequisite"),
    ]
