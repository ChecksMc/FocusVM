# Exercise nested command confinement and token-bound process recovery.
# pyright: reportPrivateUsage=false

import os
import signal
import subprocess
from pathlib import Path
from uuid import UUID

import pytest

from focusvm import cage, viewer
from focusvm.virtualization import VirtualizationUnavailable

VM = UUID(int=70)
TOKEN = UUID(int=80)
VIEWER_ARGS = [
    "/usr/bin/virt-viewer",
    "--full-screen",
    "--hotkeys",
    "toggle-fullscreen=shift+f11,release-cursor=shift+f12",
    "--connect",
    "qemu:///system",
    "--attach",
    "--uuid",
    str(VM),
]
CAGE_ARGS = ["--", *VIEWER_ARGS]


def test_cage_needs_a_local_wayland_desktop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    with pytest.raises(VirtualizationUnavailable, match="local Wayland"):
        cage.check_cage_viewer(VM)


def test_own_display_and_native_cage_are_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(cage, "wayland_display", lambda: tmp_path / "wayland-test")

    def command(vm: UUID) -> tuple[str, list[str]]:
        assert vm == VM
        return VIEWER_ARGS[0], VIEWER_ARGS[1:]

    monkeypatch.setattr(cage, "check_focus_viewer", command)
    with pytest.raises(VirtualizationUnavailable, match="Install Cage"):
        cage.check_cage_viewer(VM)
    executable = tmp_path / "cage"
    executable.symlink_to("/bin/true")
    with pytest.raises(VirtualizationUnavailable, match="native Cage"):
        cage.check_cage_viewer(VM)
    executable.unlink()
    executable.touch(mode=0o700)
    assert cage.check_cage_viewer(VM) == (str(executable), CAGE_ARGS)


def test_launch_forces_the_nested_wayland_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    display = tmp_path / "wayland-test"
    monkeypatch.setattr(cage, "_parent_display", lambda: display)
    monkeypatch.setenv("WLR_BACKENDS", "drm,libinput")
    monkeypatch.setenv("WLR_WL_OUTPUTS", "2")
    monkeypatch.setenv("DISPLAY", ":1")
    monkeypatch.setenv("_WAYLAND_DISPLAY", "ignored")
    monkeypatch.setenv("WAYLAND_SOCKET", "42")
    monkeypatch.setenv("GDK_BACKEND", "x11")
    command = ("/usr/bin/cage", CAGE_ARGS)
    log = tmp_path / "viewer.log"

    def launch(
        selected_command: tuple[str, list[str]],
        token: UUID,
        viewer_log: Path,
        label: str,
        environment: dict[str, str],
    ) -> None:
        assert selected_command == command
        assert token == TOKEN
        assert viewer_log == log and label == "Cage window"
        assert environment["WAYLAND_DISPLAY"] == str(display)
        assert environment["WLR_BACKENDS"] == "wayland"
        assert environment["WLR_WL_OUTPUTS"] == "1"
        assert environment["GDK_BACKEND"] == "wayland"
        assert {"DISPLAY", "_WAYLAND_DISPLAY", "WAYLAND_SOCKET"}.isdisjoint(environment)

    monkeypatch.setattr(cage, "launch_detached", launch)
    monkeypatch.setattr(cage, "cage_running", _running)
    cage.launch_cage_viewer(command, TOKEN, log)


def _running(token: UUID) -> bool:
    return token == TOKEN


def test_launch_reports_a_missing_nested_viewer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cage, "_parent_display", lambda: tmp_path / "wayland-test")
    monkeypatch.setattr(cage, "_STARTUP_SECONDS", 0.0)
    closed: list[UUID] = []

    def launch(*_args: object) -> None:
        pass

    def missing(_token: UUID) -> bool:
        return False

    monkeypatch.setattr(cage, "launch_detached", launch)
    monkeypatch.setattr(cage, "cage_running", missing)
    monkeypatch.setattr(cage, "close_cage", closed.append)
    with pytest.raises(viewer.ViewerError, match=r"nested viewer.*viewer\.log"):
        cage.launch_cage_viewer(
            ("/usr/bin/cage", CAGE_ARGS), TOKEN, tmp_path / "viewer.log"
        )
    assert closed == [TOKEN]


def test_close_pins_only_the_session_viewer_and_cage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(viewer, "_PROC", tmp_path)

    def register(
        process: subprocess.Popen[bytes], argv: list[str], token: UUID
    ) -> None:
        proc = tmp_path / str(process.pid)
        proc.mkdir()
        (proc / "exe").symlink_to(argv[0])
        (proc / "cmdline").write_bytes(b"\0".join(map(os.fsencode, [*argv, ""])))
        (proc / "environ").write_bytes(f"FOCUSVM_VIEWER_TOKEN={token}\0".encode())

    with (
        subprocess.Popen(["/bin/sleep", "30"]) as session_cage,
        subprocess.Popen(["/bin/sleep", "30"]) as session_viewer,
        subprocess.Popen(["/bin/sleep", "30"]) as unrelated,
    ):
        try:
            register(session_cage, ["/usr/bin/cage", *CAGE_ARGS], TOKEN)
            assert not cage.cage_running(TOKEN)
            register(session_viewer, VIEWER_ARGS, TOKEN)
            register(unrelated, ["/usr/bin/cage", *CAGE_ARGS], UUID(int=90))
            assert cage.cage_running(TOKEN)
            cage.close_cage(TOKEN)
            assert session_viewer.wait(2) == -signal.SIGTERM
            assert session_cage.wait(2) == -signal.SIGTERM
            assert unrelated.poll() is None
            assert not cage.cage_running(TOKEN)
            cage.close_cage(TOKEN)
        finally:
            for process in (session_cage, session_viewer, unrelated):
                if process.poll() is None:
                    process.terminate()
