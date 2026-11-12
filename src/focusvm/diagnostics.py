"""Read-only host checks for the FocusVM CLI and desktop manager."""

import os
import platform
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .config import ConfigPaths
from .virtualization import VirtualizationError, connect

type DiagnosticStatus = Literal["ok", "warning", "error"]


@dataclass(frozen=True, slots=True)
class Diagnostic:
    name: str
    status: DiagnosticStatus
    detail: str


def diagnose(paths: ConfigPaths, storage_pool: str | None) -> list[Diagnostic]:
    """Report prerequisites without writing files or changing libvirt objects."""
    architecture = platform.machine()
    checks = [
        Diagnostic("architecture", "ok", architecture)
        if architecture == "x86_64"
        else Diagnostic(
            "architecture",
            "error",
            f"FocusVM requires an x86_64 host. Detected {architecture or 'unknown'}.",
        ),
        _check_kvm_device(Path("/dev/kvm")),
        _check_directory("config_directory", paths.config_dir),
        _check_directory("state_directory", paths.state_dir),
        _check_directory("cache_directory", paths.cache_dir),
    ]
    checks.extend(_check_libvirt(storage_pool))
    for command in ("qemu-img", "gpgv", "openssl", "xorriso"):
        checks.append(_check_command(command, required=True))
    checks.extend(
        (
            _check_command("virt-viewer", required=False),
            _check_desktop(),
            Diagnostic(
                "guest_image",
                "warning",
                "Provide a local QCOW2 image, signed manifest, and trusted keyring to create a VM.",
            ),
        )
    )
    return checks


def _check_kvm_device(path: Path) -> Diagnostic:
    try:
        is_device = stat.S_ISCHR(path.stat().st_mode)
    except OSError as error:
        return Diagnostic(
            "kvm_device",
            "warning",
            f"Cannot inspect {path}: {error.strerror or error}. "
            "Use libvirt's capability check for the daemon's view.",
        )
    if not is_device:
        return Diagnostic(
            "kvm_device",
            "warning",
            f"Expected a KVM character device at {path}. Check host hardware virtualization.",
        )
    return Diagnostic("kvm_device", "ok", f"{path} is visible to this process.")


def _check_directory(name: str, path: Path) -> Diagnostic:
    directory = path
    entry: os.stat_result | None = None
    while entry is None:
        try:
            entry = directory.lstat()
        except FileNotFoundError:
            if directory == directory.parent:
                return Diagnostic(name, "error", f"No existing parent for {path}.")
            directory = directory.parent
        except OSError as error:
            return Diagnostic(name, "error", f"Cannot inspect {directory}: {error}")

    if directory != path and stat.S_ISLNK(entry.st_mode):
        try:
            entry = directory.stat()
        except OSError as error:
            return Diagnostic(name, "error", f"Cannot follow {directory}: {error}")
    if not stat.S_ISDIR(entry.st_mode):
        return Diagnostic(name, "error", f"Expected a directory at {directory}.")
    if directory == path and (entry.st_uid != os.getuid() or entry.st_mode & 0o022):
        return Diagnostic(
            name,
            "error",
            f"{path} must be owned by you and protected from other writers.",
        )
    if not os.access(directory, os.W_OK | os.X_OK):
        return Diagnostic(name, "error", f"Cannot write to {directory}.")
    if directory != path:
        return Diagnostic(
            name, "ok", f"Nearest existing parent for {path} is writable."
        )
    return Diagnostic(name, "ok", f"{path} is writable.")


def _check_libvirt(storage_pool: str | None) -> list[Diagnostic]:
    try:
        # Libvirt only permits domain capability queries on a regular connection.
        with connect() as backend:
            checks = [
                Diagnostic(
                    "libvirt", "ok", "Connected to qemu:///system for host queries."
                )
            ]
            try:
                backend.check_capabilities()
            except VirtualizationError as error:
                checks.append(Diagnostic("vm_capabilities", "error", str(error)))
            else:
                checks.append(
                    Diagnostic(
                        "vm_capabilities",
                        "ok",
                        "Libvirt reports KVM and local SPICE support.",
                    )
                )

            if storage_pool is None:
                checks.append(
                    Diagnostic(
                        "storage_pool",
                        "warning",
                        "Run focusvm init --storage-pool NAME to select a directory pool.",
                    )
                )
            else:
                try:
                    pool = backend.pool_status(storage_pool)
                except VirtualizationError as error:
                    checks.append(Diagnostic("storage_pool", "error", str(error)))
                else:
                    available = f"{pool.available_bytes:,} bytes available"
                    status: DiagnosticStatus = (
                        "ok" if pool.available_bytes > 0 else "error"
                    )
                    checks.append(
                        Diagnostic("storage_pool", status, f"{pool.name}: {available}.")
                    )
            return checks
    except VirtualizationError as error:
        return [
            Diagnostic("libvirt", "error", str(error)),
            Diagnostic("vm_capabilities", "warning", "Requires a libvirt connection."),
            Diagnostic("storage_pool", "warning", "Requires a libvirt connection."),
        ]


def _check_command(name: str, *, required: bool) -> Diagnostic:
    path = shutil.which(name)
    if path:
        return Diagnostic(name, "ok", path)
    status: DiagnosticStatus = "error" if required else "warning"
    return Diagnostic(name, status, f"Install {name} and make it available on PATH.")


def _check_desktop() -> Diagnostic:
    session = os.environ.get("XDG_SESSION_TYPE", "unknown")
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "unknown")
    if os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY"):
        return Diagnostic(
            "desktop", "ok", f"Display configured ({session}, {desktop})."
        )
    return Diagnostic(
        "desktop", "warning", "No graphical display is configured in this terminal."
    )
