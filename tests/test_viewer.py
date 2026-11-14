# Exercise the viewer's process identity boundaries without launching a desktop.
# pyright: reportPrivateUsage=false

import os
import signal
import subprocess
from pathlib import Path
from uuid import UUID

import pytest

from focusvm import viewer
from focusvm.virtualization import VirtualizationUnavailable

VM_ID = UUID(int=7)
TOKEN = UUID(int=8)
ATTACH = ["--connect", "qemu:///system", "--attach", "--uuid", str(VM_ID)]
FOCUS = [
    "--full-screen",
    "--hotkeys",
    "toggle-fullscreen=shift+f11,release-cursor=shift+f12",
    *ATTACH,
]


def test_local_wayland_viewer_uses_attach_fullscreen_and_exit_hotkeys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "virt-viewer"
    executable.touch(mode=0o700)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-test")
    monkeypatch.delenv("DISPLAY", raising=False)

    def is_socket(path: Path) -> bool:
        return path == tmp_path / "wayland-test"

    monkeypatch.setattr(Path, "is_socket", is_socket)
    assert viewer.console_command(VM_ID) == (str(executable), ATTACH)
    assert viewer.check_focus_viewer(VM_ID) == (str(executable), FOCUS)


def test_focus_requires_local_display_and_native_viewer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "virt-viewer"
    executable.touch(mode=0o700)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("DISPLAY", "localhost:10.0")

    with pytest.raises(VirtualizationUnavailable, match="local Wayland or X11"):
        viewer.check_focus_viewer(VM_ID)
    monkeypatch.setenv("DISPLAY", ":10.0")
    assert viewer.check_focus_viewer(VM_ID)[1] == FOCUS

    executable.unlink()
    executable.symlink_to("/bin/true")
    with pytest.raises(VirtualizationUnavailable, match="native virt-viewer"):
        viewer.check_focus_viewer(VM_ID)


def test_launch_sets_process_marker_and_reports_early_failure(tmp_path: Path) -> None:
    log = tmp_path / "viewer.log"
    log.write_text("Old viewer log", encoding="utf-8")
    log.chmod(0o644)
    with pytest.raises(viewer.ViewerError, match=r"status 7.*viewer\.log"):
        viewer.launch_focus_viewer(
            ("/bin/sh", ["-c", 'printf "%s\\n" "$FOCUSVM_VIEWER_TOKEN"; exit 7']),
            TOKEN,
            log,
        )
    contents = log.read_text(encoding="utf-8")
    assert str(TOKEN) in contents and "Old viewer log" not in contents
    assert log.stat().st_mode & 0o777 == 0o600


def test_launch_rejects_linked_log_without_modifying_target(tmp_path: Path) -> None:
    target = tmp_path / "target"
    log = tmp_path / "viewer.log"
    target.write_text("preserve", encoding="utf-8")
    os.link(target, log)

    with pytest.raises(viewer.ViewerError, match="hard link"):
        viewer.launch_focus_viewer(("/bin/true", []), TOKEN, log)
    assert target.read_text(encoding="utf-8") == "preserve"


def test_recovery_pins_and_signals_only_matching_viewer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Real pidfd and signals, with a small fixture for just the procfs metadata.
    monkeypatch.setattr(viewer, "_PROC", tmp_path)
    with (
        subprocess.Popen(["/bin/sleep", "30"]) as session_child,
        subprocess.Popen(["/bin/sleep", "30"]) as unrelated,
    ):
        try:
            for process, token in ((session_child, TOKEN), (unrelated, UUID(int=9))):
                proc = tmp_path / str(process.pid)
                proc.mkdir()
                (proc / "exe").symlink_to("/usr/bin/virt-viewer")
                (proc / "cmdline").write_bytes(
                    b"\0".join(os.fsencode(arg) for arg in ["virt-viewer", *FOCUS, ""])
                )
                (proc / "environ").write_bytes(
                    f"FOCUSVM_VIEWER_TOKEN={token}\0".encode()
                )
            assert viewer.focus_viewer_running(TOKEN)
            viewer.close_focus_viewer(TOKEN)
            assert session_child.wait(2) == -signal.SIGTERM
            assert unrelated.poll() is None
            assert not viewer.focus_viewer_running(TOKEN)
            viewer.close_focus_viewer(TOKEN)
        finally:
            for process in (session_child, unrelated):
                if process.poll() is None:
                    process.terminate()
