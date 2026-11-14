"""Command-line entry point for FocusVM."""

import argparse
import json
import sys
import warnings
from collections.abc import Sequence
from dataclasses import asdict
from getpass import GetPassWarning, getpass
from pathlib import Path
from typing import Literal
from uuid import UUID

from . import __version__
from .config import ConfigError, ConfigPaths, ConfigStore, Settings, VMDefaults
from .diagnostics import diagnose
from .gui import GUIUnavailable, launch
from .local_state import PrivateFileError
from .provisioning import ImageInput, ProvisioningError, ProvisioningUnavailable
from .sessions import (
    DEFAULT_NETWORK,
    OFFLINE_NETWORKS,
    PROFILE_ID,
    PROFILE_NAME,
    FocusSession,
    SessionError,
    SessionInputError,
    SessionService,
)
from .viewer import ViewerError
from .virtualization import VirtualizationError, VirtualizationUnavailable, VMStatus
from .vm import VMInputError, VMService, VMStateError

EXIT_INVALID_INPUT = 2
EXIT_UNAVAILABLE = 3
EXIT_OPERATION_FAILED = 4
EXIT_INTERRUPTED = 130


class _Arguments(argparse.Namespace):
    command: Literal[
        "init",
        "config",
        "doctor",
        "create",
        "list",
        "start",
        "stop",
        "status",
        "configure",
        "gui",
        "session",
        "profiles",
    ]
    session_command: Literal["start", "status", "reopen", "end"]
    storage_pool: str
    json: bool
    name: str
    image: Path
    manifest: Path
    signature: Path
    keyring: Path
    vm: UUID | None
    cpus: int | None
    memory_mib: int | None
    disk_gib: int | None
    timeout: int
    profile: str
    network: str


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="focusvm", description="Set up the FocusVM development environment."
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )

    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Create the initial configuration")
    init.add_argument(
        "--storage-pool",
        required=True,
        metavar="NAME",
        help="Name of the libvirt storage pool to use",
    )
    config = commands.add_parser("config", help="Inspect the configuration")
    config_commands = config.add_subparsers(required=True)
    config_commands.add_parser("validate", help="Check the current configuration")

    doctor = commands.add_parser(
        "doctor", help="Check local virtualization prerequisites"
    )
    _json_option(doctor)
    create = commands.add_parser(
        "create", help="Define an offline VM from a signed local image"
    )
    create.add_argument("--name", required=True, help="Name for the new VM")
    create.add_argument(
        "--image", type=Path, required=True, help="Local guest QCOW2 image"
    )
    create.add_argument(
        "--manifest", type=Path, required=True, help="Signed image manifest (JSON)"
    )
    create.add_argument(
        "--signature", type=Path, required=True, help="Detached OpenPGP signature"
    )
    create.add_argument(
        "--keyring", type=Path, required=True, help="Trusted binary OpenPGP keyring"
    )
    _resource_options(create, required=False)
    create.add_argument("--disk-gib", type=int, help="Virtual disk size in GiB")
    _json_option(create)

    listing = commands.add_parser(
        "list", help="List locally recorded VMs and creation attempts"
    )
    _json_option(listing)
    start = commands.add_parser("start", help="Boot a managed offline VM")
    _vm_options(start)
    stop = commands.add_parser("stop", help="Shut down a managed VM gracefully")
    _vm_options(stop)
    stop.add_argument(
        "--timeout",
        type=int,
        default=60,
        help="Seconds to wait for shutdown (default: 60)",
    )
    status = commands.add_parser(
        "status", help="Read the current state of a managed VM"
    )
    _vm_options(status)
    configure = commands.add_parser(
        "configure", help="Set CPU and RAM on a stopped managed VM"
    )
    _vm_options(configure)
    _resource_options(configure, required=True)
    commands.add_parser("gui", help="Open the FocusVM desktop manager")

    session = commands.add_parser("session", help="Manage the current focus session")
    sessions = session.add_subparsers(dest="session_command", required=True)
    session_start = sessions.add_parser(
        "start", help="Start a fullscreen focus session"
    )
    _vm_options(session_start)
    session_start.add_argument(
        "--profile", choices=(PROFILE_ID,), default=PROFILE_ID, help="Focus profile"
    )
    session_start.add_argument(
        "--network",
        choices=(*OFFLINE_NETWORKS, "flexible"),
        default=DEFAULT_NETWORK,
        help="Guest network policy (Flexible is unavailable)",
    )
    _json_option(sessions.add_parser("status", help="Read the current focus session"))
    _json_option(sessions.add_parser("reopen", help="Reopen the fullscreen viewer"))
    _json_option(
        sessions.add_parser("end", help="End the session and leave the VM running")
    )

    profiles = commands.add_parser("profiles", help="Inspect available focus profiles")
    profile_commands = profiles.add_subparsers(required=True)
    _json_option(
        profile_commands.add_parser("list", help="List available focus profiles")
    )
    return parser


def _json_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--json", action="store_true", help="Print machine-readable results"
    )


def _vm_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--vm", type=UUID, metavar="UUID", help="Select a VM from focusvm list"
    )
    _json_option(parser)


def _resource_options(parser: argparse.ArgumentParser, *, required: bool) -> None:
    parser.add_argument(
        "--cpus", type=int, required=required, help="Number of virtual CPUs"
    )
    parser.add_argument(
        "--memory-mib", type=int, required=required, help="Guest RAM in MiB"
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run a FocusVM command and return its exit status."""
    args = _parser().parse_args(argv, namespace=_Arguments())

    try:
        return _dispatch(args, ConfigPaths.from_environment())
    except (ConfigError, VMInputError, ProvisioningError, SessionInputError) as error:
        print(f"focusvm: {error}", file=sys.stderr)
        return EXIT_INVALID_INPUT
    except (
        VirtualizationUnavailable,
        ProvisioningUnavailable,
        GUIUnavailable,
    ) as error:
        print(f"focusvm: {error}", file=sys.stderr)
        return EXIT_UNAVAILABLE
    except (
        OSError,
        PrivateFileError,
        VirtualizationError,
        VMStateError,
        SessionError,
        ViewerError,
    ) as error:
        print(f"focusvm: {error}", file=sys.stderr)
        return EXIT_OPERATION_FAILED
    except KeyboardInterrupt:
        print("focusvm: Operation interrupted", file=sys.stderr)
        return EXIT_INTERRUPTED


def _dispatch(args: _Arguments, paths: ConfigPaths) -> int:
    if args.command == "gui":
        return launch(paths)
    if args.command == "profiles":
        if args.json:
            print(
                json.dumps(
                    [
                        {
                            "id": PROFILE_ID,
                            "name": PROFILE_NAME,
                            "network": DEFAULT_NETWORK,
                        }
                    ]
                )
            )
        else:
            print(
                f"{PROFILE_NAME} ({PROFILE_ID}) uses {DEFAULT_NETWORK} networking by default."
            )
        return 0
    store = ConfigStore(paths.config_file)
    if args.command == "init":
        if store.initialize(Settings.for_pool(args.storage_pool)):
            print(f"Created configuration at {store.path}")
        else:
            print(f"Configuration already initialized at {store.path}")
        return 0

    if args.command == "config":
        _require_config(store)
        print(f"Configuration is valid at {store.path}")
        return 0

    if args.command == "doctor":
        settings = _load_config(store)
        diagnostics = diagnose(
            paths, settings.application.storage_pool if settings else None
        )
        if args.json:
            print(json.dumps([asdict(item) for item in diagnostics]))
        else:
            for item in diagnostics:
                print(f"{item.status.upper():7} {item.name}: {item.detail}")
        return (
            EXIT_UNAVAILABLE
            if any(item.status == "error" for item in diagnostics)
            else 0
        )

    service = VMService(paths.state_dir, paths.cache_dir)
    if args.command == "session":
        return _dispatch_session(args, store, SessionService(paths.state_dir, service))
    if args.command == "list":
        records = service.list_records()
        if args.json:
            print(
                json.dumps(
                    [
                        {
                            "uuid": str(record.uuid),
                            "name": record.name,
                            "phase": record.phase,
                        }
                        for record in records
                    ]
                )
            )
        elif records:
            for record in records:
                print(f"{record.uuid}  {record.name}  {record.phase}")
        else:
            print("No VMs in the FocusVM inventory.")
        return 0

    if args.command == "create":
        settings = _require_config(store)
        defaults = settings.vm_defaults
        resources = VMDefaults(
            cpus=defaults.cpus if args.cpus is None else args.cpus,
            memory_mib=defaults.memory_mib
            if args.memory_mib is None
            else args.memory_mib,
            disk_gib=defaults.disk_gib if args.disk_gib is None else args.disk_gib,
        )
        status = service.create(
            args.name,
            ImageInput(args.image, args.manifest, args.signature, args.keyring),
            _guest_password(),
            settings.application.storage_pool,
            resources,
        )
    else:
        target = _target_vm(args.vm, store)
        if args.command == "start":
            status = service.start(target)
        elif args.command == "stop":
            status = service.stop(target, args.timeout)
        elif args.command == "configure":
            if args.cpus is None or args.memory_mib is None:
                raise VMInputError("Configure requires both --cpus and --memory-mib")
            status = service.configure(target, args.cpus, args.memory_mib)
        else:
            status = service.status(target)

    _print_status(status, json_output=args.json)
    return 0


def _dispatch_session(
    args: _Arguments, store: ConfigStore, service: SessionService
) -> int:
    session: FocusSession | None
    if args.session_command == "start":
        target = _target_vm(args.vm, store)
        if not args.json:
            _print_viewer_controls()
        session = service.start(target, args.profile, args.network)
    elif args.session_command == "reopen":
        if not args.json:
            _print_viewer_controls()
        session = service.reopen()
    elif args.session_command == "end":
        service.end()
        if args.json:
            print(json.dumps({"state": "ended"}))
        else:
            print("Focus session ended. Use focusvm stop to shut down the VM.")
        return 0
    else:
        session = service.status()
    _print_session(session, json_output=args.json)
    return 0


def _target_vm(target: UUID | None, store: ConfigStore) -> UUID | None:
    if target is None and (settings := _load_config(store)) is not None:
        return settings.application.default_vm
    return target


def _print_viewer_controls() -> None:
    print(
        "Press Shift+F12 to release input or Shift+F11 to toggle fullscreen. "
        "Run focusvm session end from a host terminal to end the session.",
        flush=True,
    )


def _print_session(session: FocusSession | None, *, json_output: bool) -> None:
    if session is None:
        print("null" if json_output else "No focus session.")
        return
    if json_output:
        print(
            json.dumps(
                {
                    "vm": str(session.vm),
                    "vm_name": session.vm_name,
                    "profile": session.profile,
                    "network": session.network,
                    "state": session.state,
                    "started_at": session.started_at,
                    "detail": session.detail,
                }
            )
        )
        return
    print(f"Focus session for {session.vm_name} ({session.vm}) is {session.state}.")
    print(f"Profile {session.profile} uses {session.network} networking.")
    if session.detail:
        print(session.detail)


def _load_config(store: ConfigStore) -> Settings | None:
    try:
        return store.load()
    except FileNotFoundError:
        return None


def _require_config(store: ConfigStore) -> Settings:
    settings = _load_config(store)
    if settings is None:
        raise ConfigError(
            f"No configuration at {store.path}. Run focusvm init --storage-pool NAME."
        )
    return settings


def _guest_password() -> str:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", GetPassWarning)
            password = getpass("Password for the guest account focusvm: ")
            confirmation = getpass("Confirm guest password: ")
    except (EOFError, GetPassWarning) as error:
        raise VMInputError(
            "Run focusvm create in a terminal to enter the guest password securely"
        ) from error
    if password != confirmation:
        raise VMInputError("Guest passwords did not match")
    return password


def _print_status(status: VMStatus, *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(asdict(status), default=str))
        return
    print(f"{status.name} ({status.uuid}) is {status.state}")
    print(f"CPU: {status.cpus} vCPUs. Memory: {status.memory_mib:,} MiB.")
    print(
        f"Disk: {status.disk_capacity_bytes:,} bytes virtual, {status.disk_allocation_bytes:,} bytes allocated."
    )
