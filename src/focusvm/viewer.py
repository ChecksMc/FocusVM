"""Launch and recover an attached local virt-viewer console."""

import os
import re
import select
import shutil
import signal
import subprocess
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager, suppress
from pathlib import Path
from threading import Thread
from uuid import UUID

from .local_state import PrivateFileError, open_private_file
from .virtualization import URI, VirtualizationUnavailable

_VIEWER = "virt-viewer"
_SESSION_MARKER_KEY = "FOCUSVM_VIEWER_TOKEN"
_ATTACH_ARGS = ("--connect", URI, "--attach", "--uuid")
_HOTKEYS = "toggle-fullscreen=shift+f11,release-cursor=shift+f12"
_FOCUS_ARGS = ("--full-screen", "--hotkeys", _HOTKEYS)
_FOCUS_ARGV = tuple(os.fsencode(argument) for argument in (*_FOCUS_ARGS, *_ATTACH_ARGS))
_PROC = Path("/proc")
_X11_SOCKETS = Path("/tmp/.X11-unix")  # noqa: S108 - Standard X11 socket location, inspected only.
_STARTUP_SECONDS = 0.5
_CLOSE_MILLISECONDS = 3000


class ViewerError(RuntimeError):
    """FocusVM could not launch, identify, or close the session viewer."""


def console_command(vm: UUID) -> tuple[str, list[str]]:
    return _viewer_executable(), [*_ATTACH_ARGS, str(vm)]


def check_focus_viewer(vm: UUID) -> tuple[str, list[str]]:
    """Check prerequisites and build the fullscreen-only console command."""
    executable = check_viewer_prerequisites()
    return executable, [*_FOCUS_ARGS, *_ATTACH_ARGS, str(vm)]


def check_viewer_prerequisites() -> str:
    executable = _viewer_executable()
    try:
        if Path(executable).resolve(strict=True).name != _VIEWER:
            raise VirtualizationUnavailable(
                "The focus session requires the native virt-viewer executable"
            )
    except OSError as error:
        raise VirtualizationUnavailable(
            "Could not resolve the virt-viewer executable"
        ) from error
    if local_display_backend() is None:
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
    return executable


def _viewer_executable() -> str:
    executable = shutil.which(_VIEWER)
    if executable is None:
        raise VirtualizationUnavailable("Install virt-viewer to open the VM console")
    return executable


def launch_focus_viewer(command: tuple[str, list[str]], token: UUID, log: Path) -> None:
    """Start a detachable viewer whose process can be recovered by token."""
    launch_detached(command, token, log, "fullscreen viewer")


def launch_detached(
    command: tuple[str, list[str]],
    token: UUID,
    log: Path,
    label: str,
    environment: Mapping[str, str] | None = None,
) -> None:
    executable, arguments = command
    env = os.environ if environment is None else environment
    try:
        with open(log, "ab", opener=open_private_file) as output:
            os.fchmod(output.fileno(), 0o600)
            output.truncate(0)
            output.write(f"Launching the FocusVM {label}.\n".encode())
            output.flush()
            process = subprocess.Popen(  # noqa: S603 - Verified local viewer or compositor.
                [executable, *arguments],
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                env={**env, _SESSION_MARKER_KEY: str(token)},
                start_new_session=True,
            )
    except (OSError, PrivateFileError) as error:
        raise ViewerError(
            f"Could not launch the {label}. Log: {log}. {error}"
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
    raise ViewerError(f"The {label} exited with status {returncode}. Log: {log}")


def focus_viewer_running(token: UUID) -> bool:
    return process_running(token, _VIEWER, matches_viewer_argv, "fullscreen viewer")


def close_focus_viewer(token: UUID) -> None:
    """Close only the session's identified viewer and wait briefly for exit."""
    close_process(token, _VIEWER, matches_viewer_argv, "fullscreen viewer")


def process_running(
    token: UUID,
    executable: str,
    matches_argv: Callable[[Sequence[bytes]], bool],
    label: str,
) -> bool:
    with _session_process(token, executable, matches_argv, label) as descriptor:
        return descriptor is not None and not _exited(descriptor)


def close_process(
    token: UUID,
    executable: str,
    matches_argv: Callable[[Sequence[bytes]], bool],
    label: str,
) -> None:
    with _session_process(token, executable, matches_argv, label) as descriptor:
        if descriptor is None:
            return
        try:
            signal.pidfd_send_signal(descriptor, signal.SIGTERM)
        except ProcessLookupError:
            return
        except (AttributeError, OSError) as error:
            raise ViewerError(
                f"Could not close the {label}. Close its window manually."
            ) from error
        if not _exited(descriptor, _CLOSE_MILLISECONDS):
            raise ViewerError(f"The {label} did not exit. Close its window manually.")


def local_display_backend() -> str | None:
    if wayland_display() is not None:
        return "Wayland"
    display = re.fullmatch(
        r"(?:unix)?:([0-9]+)(?:\.[0-9]+)?", os.environ.get("DISPLAY", "")
    )
    if display is not None:
        with suppress(OSError):
            if (_X11_SOCKETS / f"X{display[1]}").is_socket():
                return "X11"
    return None


def wayland_display() -> Path | None:
    name = os.environ.get("WAYLAND_DISPLAY")
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if name:
        socket = Path(name)
        if socket.is_absolute() or runtime:
            socket = socket if socket.is_absolute() else Path(runtime or "") / socket
            with suppress(OSError):
                if socket.is_socket():
                    return socket
    return None


@contextmanager
def _session_process(
    token: UUID,
    executable: str,
    matches_argv: Callable[[Sequence[bytes]], bool],
    label: str,
) -> Generator[int | None]:
    marker = os.fsencode(f"{_SESSION_MARKER_KEY}={token}")
    descriptor: int | None = None
    try:
        try:
            for directory in _PROC.iterdir():
                if directory.name.isascii() and directory.name.isdecimal():
                    candidate = _pin_process(
                        directory, marker, executable, matches_argv
                    )
                    if candidate is None:
                        continue
                    if descriptor is not None and not _exited(descriptor):
                        os.close(candidate)
                        raise ViewerError(
                            f"Multiple processes match the session's {label}. Close their windows manually."
                        )
                    if descriptor is not None:
                        os.close(descriptor)
                    descriptor = candidate
        except (AttributeError, OSError) as error:
            raise ViewerError(
                f"Could not verify the {label} process. Close its window manually."
            ) from error
        yield descriptor
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _pin_process(
    directory: Path,
    marker: bytes,
    executable: str,
    matches_argv: Callable[[Sequence[bytes]], bool],
) -> int | None:
    try:
        if directory.stat().st_uid != os.getuid():
            return None
        if not matches_argv((directory / "cmdline").read_bytes().split(b"\0")):
            return None
        descriptor = os.pidfd_open(int(directory.name))
    except (FileNotFoundError, ProcessLookupError):
        return None

    # Match again after pinning the PID, so PID reuse cannot redirect a signal.
    keep = False
    try:
        if _exited(descriptor) or directory.stat().st_uid != os.getuid():
            return None
        if not matches_argv((directory / "cmdline").read_bytes().split(b"\0")):
            return None
        if marker not in (directory / "environ").read_bytes().split(b"\0"):
            return None
        actual = (directory / "exe").readlink().name.removesuffix(" (deleted)")
        keep = actual == executable and not _exited(descriptor)
        return descriptor if keep else None
    except (FileNotFoundError, ProcessLookupError):
        return None
    finally:
        if not keep:
            os.close(descriptor)


def matches_viewer_argv(arguments: Sequence[bytes]) -> bool:
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
