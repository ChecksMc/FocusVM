"""Read-only host checks for the FocusVM CLI and desktop manager."""

import os
import platform
import shutil
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .config import ConfigError, ConfigPaths, ConfigStore
from .local_state import PrivateFileError, check_private_directory
from .viewer import check_viewer_prerequisites, local_display_backend
from .virtualization import VirtualizationError, VirtualizationUnavailable, connect

type DiagnosticStatus = Literal["ok", "warning", "error"]

_REQUIRED_TOOLS = {
    "qemu-img": "qemu-img",
    "gpgv": "gnupg2-verify",
    "openssl": "openssl",
    "xorriso": "xorriso",
}
_SHAREABLE_DETAILS = {
    "architecture": "x86-64 host architecture",
    "configuration": "FocusVM settings",
    "kvm_device": "Local KVM device visibility",
    "config_directory": "Configuration directory access",
    "state_directory": "State directory access",
    "cache_directory": "Cache directory access",
    "data_directory": "Data directory access",
    "libvirt": "System libvirt connection",
    "vm_capabilities": "KVM and local SPICE support",
    "storage_pool": "Directory storage pool readiness",
    "desktop": "Local graphical session",
    "focus_viewer": "Focus viewer prerequisites",
    "guest_image": "Signed guest image required separately",
    **{tool: "Executable availability" for tool in (*_REQUIRED_TOOLS, "virt-viewer")},
}


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
        _check_directory("data_directory", paths.data_dir),
    ]
    checks.extend(_check_libvirt(storage_pool))
    for command in _REQUIRED_TOOLS:
        checks.append(_check_command(command, required=True))
    checks.extend(
        (
            _check_command("virt-viewer", required=False),
            _check_desktop(),
            _check_focus_viewer(),
            Diagnostic(
                "guest_image",
                "warning",
                "Provide a local QCOW2 image, signed manifest, signature, and trusted keyring. "
                "See the guest build guide before creating a VM.",
            ),
        )
    )
    return checks


def diagnose_configured(paths: ConfigPaths) -> list[Diagnostic]:
    """Report configuration errors alongside the remaining independent checks."""
    store = ConfigStore(paths.config_file)
    try:
        settings = store.load()
    except FileNotFoundError:
        configuration = Diagnostic(
            "configuration", "warning", f"Settings have not been saved to {store.path}."
        )
    except (ConfigError, OSError) as error:
        configuration = Diagnostic("configuration", "error", f"{store.path}: {error}")
    else:
        return [
            Diagnostic("configuration", "ok", f"Read settings from {store.path}."),
            *diagnose(paths, settings.application.storage_pool),
        ]
    return [configuration, *diagnose(paths, None)]


def redact_diagnostics(checks: Sequence[Diagnostic]) -> list[Diagnostic]:
    """Keep known check identifiers and results, excluding all raw details."""
    return [
        Diagnostic(
            item.name if item.name in _SHAREABLE_DETAILS else "check",
            item.status,
            _SHAREABLE_DETAILS.get(item.name, "Host prerequisite"),
        )
        for item in checks
    ]


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
    if directory == path:
        try:
            check_private_directory(path)
        except (OSError, PrivateFileError) as error:
            return Diagnostic(name, "error", str(error))
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
                detail = str(error)
                if "spice" in detail.casefold():
                    detail += ". Install a QEMU build with SPICE support, such as qemu-ui-spice-core on Fedora."
                else:
                    detail += ". Check CPU virtualization or nested KVM, then run virt-host-validate qemu."
                checks.append(Diagnostic("vm_capabilities", "error", detail))
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
                        "Run focusvm setup to find a usable pool, or focusvm setup --storage-pool NAME to choose one.",
                    )
                )
            else:
                try:
                    pool = backend.pool_status(storage_pool)
                except VirtualizationError as error:
                    checks.append(
                        Diagnostic(
                            "storage_pool",
                            "error",
                            f"{error}. Run virsh --connect qemu:///system pool-list --all "
                            "and confirm the configured pool is an active directory pool.",
                        )
                    )
                else:
                    available = f"{pool.available_bytes:,} bytes available"
                    status: DiagnosticStatus = (
                        "ok" if pool.available_bytes > 0 else "error"
                    )
                    if status == "error":
                        available += ". Free space in the pool or choose another pool in Settings"
                    checks.append(
                        Diagnostic("storage_pool", status, f"{pool.name}: {available}.")
                    )
            return checks
    except VirtualizationError as error:
        return [
            Diagnostic("libvirt", "error", _libvirt_help(str(error))),
            Diagnostic("vm_capabilities", "warning", "Requires a libvirt connection."),
            Diagnostic("storage_pool", "warning", "Requires a libvirt connection."),
        ]


def _libvirt_help(detail: str) -> str:
    message = detail.casefold()
    if "python bindings" in message:
        hint = (
            "On Fedora, install python3-libvirt and use the system Python or a "
            "virtual environment created with --system-site-packages."
        )
    elif any(word in message for word in ("permission", "denied", "auth", "timeout")):
        hint = (
            "Run FocusVM from your regular desktop session and check access with "
            "virsh --connect qemu:///system list. Ask your administrator about libvirt authorization."
        )
    else:
        hint = (
            "On hosts using systemd, check with systemctl status virtqemud.socket "
            "virtstoraged.socket or systemctl status libvirtd.socket libvirtd.service."
        )
    return f"{detail.rstrip('.')}. {hint}"


def _check_command(name: str, *, required: bool) -> Diagnostic:
    path = shutil.which(name)
    if path:
        return Diagnostic(name, "ok", path)
    status: DiagnosticStatus = "error" if required else "warning"
    package = _REQUIRED_TOOLS.get(name, name)
    return Diagnostic(
        name,
        status,
        f"Install {name} and make it available on PATH. On Fedora, use sudo dnf install {package}.",
    )


def _check_desktop() -> Diagnostic:
    session = os.environ.get("XDG_SESSION_TYPE", "unknown")
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "unknown")
    if backend := local_display_backend():
        return Diagnostic(
            "desktop", "ok", f"Local {backend} display ({session} session, {desktop})."
        )
    if os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY"):
        return Diagnostic(
            "desktop",
            "warning",
            "No usable local display. Check the Wayland socket or open a local desktop terminal.",
        )
    return Diagnostic(
        "desktop", "warning", "No graphical display is configured in this terminal."
    )


def _check_focus_viewer() -> Diagnostic:
    try:
        check_viewer_prerequisites()
    except VirtualizationUnavailable as error:
        return Diagnostic("focus_viewer", "warning", str(error))
    return Diagnostic(
        "focus_viewer",
        "ok",
        "Viewer, local display, and Linux process controls are available.",
    )
