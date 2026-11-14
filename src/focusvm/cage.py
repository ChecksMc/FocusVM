"""Optional virt-viewer inside a nested Wayland Cage compositor."""

import os
import shutil
from collections.abc import Sequence
from pathlib import Path
from time import monotonic, sleep
from uuid import UUID

from .viewer import (
    ViewerError,
    check_focus_viewer,
    close_focus_viewer,
    close_process,
    focus_viewer_running,
    launch_detached,
    matches_viewer_argv,
    process_running,
    wayland_display,
)
from .virtualization import VirtualizationUnavailable

_CAGE = "cage"
_LABEL = "Cage window"
_STARTUP_SECONDS = 3.0


def check_cage_viewer(vm: UUID) -> tuple[str, list[str]]:
    _parent_display()
    path = shutil.which(_CAGE)
    if path is None:
        raise VirtualizationUnavailable("Install Cage to use the optional Cage viewer")
    try:
        executable = Path(path).resolve(strict=True)
    except OSError as error:
        raise VirtualizationUnavailable(
            "Could not resolve the Cage executable"
        ) from error
    if executable.name != _CAGE:
        raise VirtualizationUnavailable(
            "The Cage viewer requires the native Cage executable"
        )
    viewer, arguments = check_focus_viewer(vm)
    return str(executable), ["--", viewer, *arguments]


def launch_cage_viewer(command: tuple[str, list[str]], token: UUID, log: Path) -> None:
    display = _parent_display()
    environment = dict(os.environ)
    for variable in ("DISPLAY", "WAYLAND_SOCKET", "_WAYLAND_DISPLAY"):
        environment.pop(variable, None)
    # Force a nested Wayland window. Cage sets the client's display to its own socket.
    environment.update(
        WAYLAND_DISPLAY=str(display),
        WLR_BACKENDS="wayland",
        WLR_WL_OUTPUTS="1",
        GDK_BACKEND="wayland",
    )
    launch_detached(command, token, log, _LABEL, environment)
    deadline = monotonic() + _STARTUP_SECONDS
    while True:
        if cage_running(token):
            return
        if monotonic() >= deadline:
            close_cage(token)
            raise ViewerError(f"Cage could not start the nested viewer. Log: {log}")
        sleep(0.1)


def cage_running(token: UUID) -> bool:
    return process_running(
        token, _CAGE, _matches_cage, _LABEL
    ) and focus_viewer_running(token)


def close_cage(token: UUID) -> None:
    """Close the marked viewer first so Cage can reap its client and exit."""
    close_focus_viewer(token)
    close_process(token, _CAGE, _matches_cage, _LABEL)


def _parent_display() -> Path:
    display = wayland_display()
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR", ""))
    if (
        display is None
        or not display.is_absolute()
        or not runtime.is_absolute()
        or not runtime.is_dir()
    ):
        raise VirtualizationUnavailable(
            "Open a local Wayland desktop to use the Cage viewer"
        )
    return display


def _matches_cage(arguments: Sequence[bytes]) -> bool:
    return (
        len(arguments) > 2
        and Path(os.fsdecode(arguments[0])).name == _CAGE
        and arguments[1] == b"--"
        and matches_viewer_argv(arguments[2:])
    )
