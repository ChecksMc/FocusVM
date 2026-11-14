"""Launch and recover an attached local virt-viewer console."""

import os
import re
import select
import shutil
import signal
import subprocess
from collections.abc import Generator, Sequence
from contextlib import contextmanager, suppress
from pathlib import Path
from threading import Thread
from uuid import UUID

from .local_state import PrivateFileError, open_owned_file
from .virtualization import URI, VirtualizationUnavailable

_VIEWER = "virt-viewer"
_SESSION_MARKER_KEY = "FOCUSVM_VIEWER_TOKEN"
_ATTACH_ARGS = ("--connect", URI, "--attach", "--uuid")
_HOTKEYS = "toggle-fullscreen=shift+f11,release-cursor=shift+f12"
_FOCUS_ARGS = ("--full-screen", "--hotkeys", _HOTKEYS)
_FOCUS_ARGV = tuple(os.fsencode(argument) for argument in (*_FOCUS_ARGS, *_ATTACH_ARGS))
_PROC = Path("/proc")
_STARTUP_SECONDS = 0.5
_CLOSE_MILLISECONDS = 3000


class ViewerError(RuntimeError):
    """FocusVM could not launch, identify, or close the session viewer."""


def console_command(vm: UUID) -> tuple[str, list[str]]:
    executable = shutil.which(_VIEWER)
    if executable is None:
        raise VirtualizationUnavailable("Install virt-viewer to open the VM console")
    return executable, [*_ATTACH_ARGS, str(vm)]


def check_focus_viewer(vm: UUID) -> tuple[str, list[str]]:
    """Check prerequisites and build the fullscreen-only console command."""
    executable, arguments = console_command(vm)
    try:
        if Path(executable).resolve(strict=True).name != _VIEWER:
            raise VirtualizationUnavailable(
                "The focus session requires the native virt-viewer executable"
            )
    except OSError as error:
        raise VirtualizationUnavailable(
            "Could not resolve the virt-viewer executable"
        ) from error
    if not _local_display():
        raise VirtualizationUnavailable(
            "Start the focus session from a local Wayland or X11 desktop"
        )
    try:
        descriptor = os.pidfd_open(os.getpid())
        try:
            signal.pidfd_send_signal(descriptor, 0)
        finally:
            os.close(descriptor)
    except (AttributeError, OSError) as error:
        raise VirtualizationUnavailable(
            "This environment cannot safely manage a detached focus viewer (pidfd unavailable)"
        ) from error
    return executable, [*_FOCUS_ARGS, *arguments]


def launch_focus_viewer(command: tuple[str, list[str]], token: UUID, log: Path) -> None:
    """Start a detachable viewer whose process can be recovered by token."""
    executable, arguments = command
    try:
        with open(log, "ab", opener=open_owned_file) as output:
            if os.fstat(output.fileno()).st_nlink != 1:
                raise ViewerError(f"Viewer log cannot be a hard link: {log}")
            os.fchmod(output.fileno(), 0o600)
            output.truncate(0)
            output.write(b"Launching the FocusVM fullscreen viewer.\n")
            output.flush()
            process = subprocess.Popen(  # noqa: S603 - Started from console_command.
                [executable, *arguments],
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                env={**os.environ, _SESSION_MARKER_KEY: str(token)},
                start_new_session=True,
            )
    except (OSError, PrivateFileError) as error:
        raise ViewerError(
            f"Could not launch the fullscreen viewer. Log: {log}. {error}"
        ) from error

    try:
        returncode = process.wait(timeout=_STARTUP_SECONDS)
    except subprocess.TimeoutExpired:
        return
    finally:
        if process.returncode is None:
            Thread(
                target=process.wait, name="focusvm-viewer-reaper", daemon=True
            ).start()
    raise ViewerError(
        f"The fullscreen viewer exited with status {returncode}. Log: {log}"
    )


def focus_viewer_running(token: UUID) -> bool:
    with _focus_viewer(token) as descriptor:
        return descriptor is not None and not _exited(descriptor)


def close_focus_viewer(token: UUID) -> None:
    """Close only the session's identified viewer and wait briefly for exit."""
    with _focus_viewer(token) as descriptor:
        if descriptor is None:
            return
        try:
            signal.pidfd_send_signal(descriptor, signal.SIGTERM)
        except ProcessLookupError:
            return
        except (AttributeError, OSError) as error:
            raise ViewerError(
                "Could not close the fullscreen viewer. Close its window manually."
            ) from error
        if not _exited(descriptor, _CLOSE_MILLISECONDS):
            raise ViewerError(
                "The fullscreen viewer did not exit. Close its window manually."
            )


def _local_display() -> bool:
    name = os.environ.get("WAYLAND_DISPLAY")
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if name:
        socket = Path(name)
        if socket.is_absolute() or runtime:
            socket = socket if socket.is_absolute() else Path(runtime or "") / socket
            with suppress(OSError):
                if socket.is_socket():
                    return True
    display = os.environ.get("DISPLAY", "")
    return re.fullmatch(r"(?:unix)?:[0-9]+(?:\.[0-9]+)?", display) is not None


@contextmanager
def _focus_viewer(token: UUID) -> Generator[int | None]:
    marker = os.fsencode(f"{_SESSION_MARKER_KEY}={token}")
    descriptor: int | None = None
    try:
        try:
            for directory in _PROC.iterdir():
                if directory.name.isascii() and directory.name.isdecimal():
                    candidate = _pin_viewer(directory, marker)
                    if candidate is None:
                        continue
                    if descriptor is not None and not _exited(descriptor):
                        os.close(candidate)
                        raise ViewerError(
                            "Multiple fullscreen viewers match the session. Close their windows manually."
                        )
                    if descriptor is not None:
                        os.close(descriptor)
                    descriptor = candidate
        except (AttributeError, OSError) as error:
            raise ViewerError(
                "Could not verify the fullscreen viewer process. Close its window manually."
            ) from error
        yield descriptor
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _pin_viewer(directory: Path, marker: bytes) -> int | None:
    try:
        if directory.stat().st_uid != os.getuid():
            return None
        if not _matches_argv((directory / "cmdline").read_bytes().split(b"\0")):
            return None
        descriptor = os.pidfd_open(int(directory.name))
    except (FileNotFoundError, ProcessLookupError):
        return None

    # Match again after pinning the PID, so PID reuse cannot redirect a signal.
    keep = False
    try:
        if _exited(descriptor) or directory.stat().st_uid != os.getuid():
            return None
        if not _matches_argv((directory / "cmdline").read_bytes().split(b"\0")):
            return None
        if marker not in (directory / "environ").read_bytes().split(b"\0"):
            return None
        executable = (directory / "exe").readlink().name.removesuffix(" (deleted)")
        keep = executable == _VIEWER and not _exited(descriptor)
        return descriptor if keep else None
    except (FileNotFoundError, ProcessLookupError):
        return None
    finally:
        if not keep:
            os.close(descriptor)


def _matches_argv(arguments: Sequence[bytes]) -> bool:
    if (
        len(arguments) != len(_FOCUS_ARGV) + 3
        or Path(os.fsdecode(arguments[0])).name != _VIEWER
        or tuple(arguments[1:-2]) != _FOCUS_ARGV
        or arguments[-1]
    ):
        return False
    try:
        identifier = os.fsdecode(arguments[-2])
        return str(UUID(identifier)) == identifier
    except ValueError:
        return False


def _exited(descriptor: int, timeout: int = 0) -> bool:
    poller = select.poll()
    poller.register(descriptor, select.POLLIN)
    return bool(poller.poll(timeout))
