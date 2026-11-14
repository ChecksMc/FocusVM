"""Command-line entry point for FocusVM."""

import argparse
import json
import subprocess
import sys
import warnings
from collections.abc import Sequence
from dataclasses import asdict
from datetime import date
from getpass import GetPassWarning, getpass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal
from uuid import UUID

from . import __version__
from .backup import BackupError, backup_disk
from .config import ConfigError, ConfigPaths, ConfigStore, Settings, VMDefaults
from .diagnostics import diagnose_configured, redact_diagnostics
from .git_sync import (
    GitSyncError,
    GitSyncInputError,
    create_bundle,
    import_bundle,
    sync_remote,
)
from .guest_export import GuestExportError, export_bundle
from .gui import GUIUnavailable, launch
from .journal import Journal, JournalEntry, JournalError, JournalInputError
from .local_state import PrivateFileError
from .packages import PackageInputError, build_wheel_media
from .profiles import PROFILES
from .provisioning import (
    ImageInput,
    ProvisioningError,
    ProvisioningUnavailable,
    stage_image,
)
from .research import Research, ResearchError, ResearchInputError
from .resources import Resource, ResourceError, ResourceLibrary, ResourceUnavailable
from .sessions import (
    OFFLINE_NETWORKS,
    PROFILE_ID,
    FocusSession,
    SessionError,
    SessionInputError,
    SessionService,
)
from .setup import setup_host
from .tracking import (
    ActivityQueue,
    TrackingError,
    TrackingInputError,
    forget_api_key,
    read_api_key,
    save_api_key,
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
        "setup",
        "config",
        "doctor",
        "create",
        "verify-image",
        "backup",
        "list",
        "start",
        "stop",
        "status",
        "configure",
        "gui",
        "session",
        "profiles",
        "resources",
        "sync",
        "tracking",
        "journal",
        "research",
        "packages",
    ]
    session_command: Literal["start", "status", "reopen", "end"]
    storage_pool: str
    setup_pool: str | None
    redact: bool
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
    profile: str | None
    network: str | None
    duration_minutes: int | None
    cage: bool
    resource_action: Literal[
        "add", "list", "search", "remove", "open", "build", "attach", "eject"
    ]
    sync_action: Literal["bundle", "import", "export-guest", "fetch", "push"]
    tracking_action: Literal[
        "enable",
        "disable",
        "status",
        "record",
        "clear",
        "store-key",
        "forget-key",
        "sync",
    ]
    path: Path
    output: Path
    repo: Path
    branch: str
    remote: str
    resource_id: str
    resource_ids: list[str]
    query: str
    projects: list[str]
    project: str
    entity: str
    timestamp: float | None
    prompt_key: bool
    guest_path: str
    journal_action: Literal["record", "list", "remove", "stats"]
    research_action: Literal["hosts", "allow", "revoke", "save"]
    package_action: Literal["build", "attach"]
    entry_id: str
    day: date
    journal_minutes: int
    completed: list[str]
    problems: list[str]
    solved: list[str]
    references: list[str]
    notes: str
    host: str
    url: str
    wheels: list[Path]


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
    setup = commands.add_parser("setup", help="Check the host and configure FocusVM")
    setup.add_argument(
        "--storage-pool",
        dest="setup_pool",
        metavar="NAME",
        help="Choose a pool for first-time setup",
    )
    _json_option(setup)
    config = commands.add_parser("config", help="Inspect the configuration")
    config_commands = config.add_subparsers(required=True)
    config_commands.add_parser("validate", help="Check the current configuration")

    doctor = commands.add_parser(
        "doctor", help="Check local virtualization prerequisites"
    )
    _json_option(doctor)
    doctor.add_argument(
        "--redact",
        action="store_true",
        help="Omit host-specific details from the report",
    )
    create = commands.add_parser(
        "create", help="Define an offline VM from a signed local image"
    )
    create.add_argument("--name", required=True, help="Name for the new VM")
    _image_options(create)
    _resource_options(create, required=False)
    create.add_argument("--disk-gib", type=int, help="Virtual disk size in GiB")
    _json_option(create)
    verify_image = commands.add_parser(
        "verify-image", help="Verify guest image inputs without connecting to libvirt"
    )
    _image_options(verify_image)
    _json_option(verify_image)
    backup = commands.add_parser(
        "backup", help="Export a standalone disk copy of a stopped VM"
    )
    _vm_options(backup)
    backup.add_argument(
        "--output", type=Path, required=True, help="New QCOW2 output path"
    )

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
        "--profile",
        choices=PROFILES,
        help="Focus profile (uses the configured default)",
    )
    session_start.add_argument(
        "--network",
        choices=(*OFFLINE_NETWORKS, "flexible"),
        help="Guest network policy (Flexible is unavailable)",
    )
    session_start.add_argument(
        "--duration-minutes", type=int, help="Optional Contest timer"
    )
    session_start.add_argument(
        "--cage", action="store_true", help="Use nested Cage on Wayland"
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
    _maintenance_parsers(
        commands.add_parser("resources", help="Manage offline reference files"),
        commands.add_parser("sync", help="Transfer Git commits"),
        commands.add_parser("tracking", help="Manage opt-in WakaTime activity"),
    )
    _advanced_parsers(
        commands.add_parser("journal", help="Record and summarize local sessions"),
        commands.add_parser(
            "research", help="Save approved HTTPS references for offline use"
        ),
        commands.add_parser("packages", help="Prepare offline Python wheel media"),
    )
    return parser


def _advanced_parsers(
    journal: argparse.ArgumentParser,
    research: argparse.ArgumentParser,
    packages: argparse.ArgumentParser,
) -> None:
    entries = journal.add_subparsers(dest="journal_action", required=True)
    record = entries.add_parser("record", help="Write a local journal entry")
    record.add_argument("--project", required=True)
    record.add_argument(
        "--date", dest="day", type=date.fromisoformat, default=date.today()
    )
    record.add_argument("--minutes", dest="journal_minutes", required=True, type=int)
    for name in ("completed", "problems", "solved", "references"):
        record.add_argument(f"--{name}", action="append", default=[])
    record.add_argument("--notes", default="")
    _json_option(record)
    for name, help_text in (
        ("list", "List entries"),
        ("stats", "Summarize recorded time"),
    ):
        subcommand = entries.add_parser(name, help=help_text)
        subcommand.add_argument("--project")
        _json_option(subcommand)
    remove = entries.add_parser("remove", help="Delete one journal entry")
    remove.add_argument("entry_id")
    _json_option(remove)

    sources = research.add_subparsers(dest="research_action", required=True)
    _json_option(sources.add_parser("hosts", help="List approved exact hostnames"))
    for name, help_text in (
        ("allow", "Approve a public hostname"),
        ("revoke", "Revoke a hostname"),
    ):
        host = sources.add_parser(name, help=help_text)
        host.add_argument("host")
        _json_option(host)
    save = sources.add_parser("save", help="Save one page as plain text in Markdown")
    save.add_argument("url")
    save.add_argument("--output", type=Path, required=True)
    _json_option(save)

    package_actions = packages.add_subparsers(dest="package_action", required=True)
    build = package_actions.add_parser(
        "build", help="Build a disc from selected wheel files"
    )
    build.add_argument("--output", type=Path, required=True)
    _json_option(build)
    attach = package_actions.add_parser(
        "attach", help="Attach a wheel disc to a stopped VM"
    )
    _vm_options(attach)
    for action in (build, attach):
        action.add_argument("wheels", type=Path, nargs="+")


def _maintenance_parsers(
    resources: argparse.ArgumentParser,
    sync: argparse.ArgumentParser,
    tracking: argparse.ArgumentParser,
) -> None:
    resource_actions = resources.add_subparsers(dest="resource_action", required=True)
    add = resource_actions.add_parser("add", help="Copy a local file into the library")
    add.add_argument("path", type=Path)
    _json_option(add)
    _json_option(resource_actions.add_parser("list", help="List library files"))
    search = resource_actions.add_parser("search", help="Find library files by name")
    search.add_argument("query")
    _json_option(search)
    remove = resource_actions.add_parser("remove", help="Remove the library copy")
    remove.add_argument("resource_id")
    _json_option(remove)
    resource_actions.add_parser(
        "open", help="Open a reference on the host"
    ).add_argument("resource_id")
    build = resource_actions.add_parser("build", help="Write a read-only reference ISO")
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("resource_ids", nargs="+")
    attach = resource_actions.add_parser(
        "attach", help="Attach selected references to a stopped VM"
    )
    attach.add_argument("resource_ids", nargs="+")
    _vm_options(attach)
    _vm_options(
        resource_actions.add_parser("eject", help="Eject references from a stopped VM")
    )

    git_actions = sync.add_subparsers(dest="sync_action", required=True)
    bundle = git_actions.add_parser(
        "bundle", help="Export a branch as an offline Git bundle"
    )
    bundle.add_argument("--output", type=Path, required=True)
    receive = git_actions.add_parser(
        "import", help="Import a bundle into a separate Git ref"
    )
    receive.add_argument("path", type=Path)
    guest_export = git_actions.add_parser(
        "export-guest", help="Copy a bundle from a stopped VM"
    )
    guest_export.add_argument("guest_path")
    guest_export.add_argument("--output", type=Path, required=True)
    _vm_options(guest_export)
    fetch = git_actions.add_parser(
        "fetch", help="Fetch one branch into a separate Git ref"
    )
    push = git_actions.add_parser("push", help="Push one local branch without force")
    for action in (bundle, receive, fetch, push):
        action.add_argument("--repo", type=Path, required=True)
        action.add_argument("--branch", required=True)
    for action in (fetch, push):
        action.add_argument("--remote", required=True)

    activity_actions = tracking.add_subparsers(dest="tracking_action", required=True)
    enable = activity_actions.add_parser(
        "enable", help="Enable recording for named projects"
    )
    enable.add_argument("projects", nargs="+")
    activity_actions.add_parser("disable", help="Disable recording and uploading")
    activity_actions.add_parser("clear", help="Discard all queued local activity")
    _json_option(
        activity_actions.add_parser(
            "status", help="Preview selected projects and queued files"
        )
    )
    record = activity_actions.add_parser(
        "record", help="Queue an explicit file heartbeat locally"
    )
    record.add_argument("--project", required=True)
    record.add_argument(
        "--file", dest="entity", required=True, help="Project-relative file path"
    )
    record.add_argument(
        "--time", dest="timestamp", type=float, help="Unix timestamp (default: now)"
    )
    activity_actions.add_parser(
        "store-key", help="Store an API key in the desktop keyring"
    )
    activity_actions.add_parser("forget-key", help="Remove the stored API key")
    send = activity_actions.add_parser(
        "sync", help="Send queued events for enabled projects to WakaTime"
    )
    send.add_argument(
        "--prompt-key",
        action="store_true",
        help="Enter an API key for this upload only",
    )


def _json_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--json", action="store_true", help="Print machine-readable results"
    )


def _vm_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--vm", type=UUID, metavar="UUID", help="Select a VM from focusvm list"
    )
    _json_option(parser)


def _image_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--image", type=Path, required=True, help="Local guest QCOW2 image"
    )
    parser.add_argument(
        "--manifest", type=Path, required=True, help="Signed image manifest (JSON)"
    )
    parser.add_argument(
        "--signature", type=Path, required=True, help="Detached OpenPGP signature"
    )
    parser.add_argument(
        "--keyring", type=Path, required=True, help="Trusted binary OpenPGP keyring"
    )


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
    except (
        ConfigError,
        VMInputError,
        ProvisioningError,
        SessionInputError,
        ResourceError,
        GitSyncInputError,
        TrackingInputError,
        JournalInputError,
        ResearchInputError,
        PackageInputError,
    ) as error:
        print(f"focusvm: {error}", file=sys.stderr)
        return EXIT_INVALID_INPUT
    except (
        VirtualizationUnavailable,
        ProvisioningUnavailable,
        GUIUnavailable,
        ResourceUnavailable,
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
        GitSyncError,
        GuestExportError,
        BackupError,
        TrackingError,
        JournalError,
        ResearchError,
        subprocess.SubprocessError,
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
        profiles = [{"id": key, **asdict(profile)} for key, profile in PROFILES.items()]
        if args.json:
            print(json.dumps(profiles))
        else:
            for key, profile in PROFILES.items():
                print(
                    f"{profile.name} ({key}) uses {profile.network} networking by default."
                )
        return 0
    if args.command == "journal":
        return _dispatch_journal(args, Journal(paths.state_dir))
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

    if args.command == "setup":
        result = setup_host(paths, args.setup_pool)
        if args.json:
            print(json.dumps({**asdict(result), "ready": result.ready}, default=str))
        else:
            for item in result.checks:
                if item.status != "ok":
                    print(f"{item.status.upper():7} {item.name}: {item.detail}")
            if result.ready:
                action = "Saved" if result.created else "Using existing"
                print(f"{action} FocusVM settings at {result.path}.")
                print(f"Storage pool: {result.storage_pool}")
                print("Open FocusVM from the app menu or run focusvm gui.")
            else:
                print(
                    "Resolve the failed checks and run focusvm setup again.",
                    file=sys.stderr,
                )
        return 0 if result.ready else EXIT_UNAVAILABLE

    if args.command == "doctor":
        diagnostics = diagnose_configured(paths)
        if args.redact:
            diagnostics = redact_diagnostics(diagnostics)
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

    if args.command == "verify-image":
        source = ImageInput(args.image, args.manifest, args.signature, args.keyring)
        with stage_image(source, staging_dir=paths.cache_dir) as verified:
            if args.json:
                print(
                    json.dumps(
                        {
                            "version": verified.version,
                            "sha256": verified.sha256,
                            "virtual_size_bytes": verified.virtual_size_bytes,
                        }
                    )
                )
            else:
                print(f"Verified {verified.version} ({verified.sha256})")
        return 0

    service = VMService(paths.state_dir, paths.cache_dir)
    if args.command == "backup":
        sessions = SessionService(paths.state_dir, service)
        with (
            sessions.outside_session(),
            service.offline_disk(_target_vm(args.vm, store)) as disk,
        ):
            _print_output(backup_disk(disk, args.output), args.json)
        return 0
    if args.command in ("packages", "research"):
        sessions = SessionService(paths.state_dir, service)
        if args.command == "research":
            return _dispatch_research(args, Research(paths.config_dir), sessions)
        if args.package_action == "build":
            _print_output(build_wheel_media(args.wheels, args.output), args.json)
        else:
            target = _target_vm(args.vm, store)
            with (
                sessions.outside_session(),
                TemporaryDirectory(prefix="focusvm-wheels-") as directory,
            ):
                image = build_wheel_media(args.wheels, Path(directory) / "wheels.iso")
                status = service.attach_resources(target, image)
            _print_status(status, json_output=args.json)
        return 0
    if args.command in ("resources", "sync", "tracking"):
        sessions = SessionService(paths.state_dir, service)
        if args.command == "resources":
            return _dispatch_resources(
                args, ResourceLibrary(paths.data_dir), store, service, sessions
            )
        if args.command == "sync":
            return _dispatch_git(args, sessions, service, store)
        return _dispatch_tracking(args, ActivityQueue(paths.state_dir), sessions)
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


def _dispatch_resources(
    args: _Arguments,
    library: ResourceLibrary,
    store: ConfigStore,
    service: VMService,
    sessions: SessionService,
) -> int:
    action = args.resource_action
    if action == "add":
        _print_resources((library.add(args.path),), json_output=args.json)
    elif action == "list":
        _print_resources(library.list(), json_output=args.json)
    elif action == "search":
        _print_resources(library.search(args.query), json_output=args.json)
    elif action == "remove":
        _print_resources((library.remove(args.resource_id),), json_output=args.json)
    elif action == "open":
        executable, options = library.open_command(args.resource_id)
        subprocess.Popen(  # noqa: S603 - Selected local document viewer.
            [executable, *options], stdin=subprocess.DEVNULL, start_new_session=True
        )
    elif action == "build":
        print(library.build_iso(args.resource_ids, args.output))
    else:
        target = _target_vm(args.vm, store)
        with sessions.outside_session():
            if action == "attach":
                with TemporaryDirectory(prefix="focusvm-references-") as directory:
                    iso = library.build_iso(
                        args.resource_ids, Path(directory) / "references.iso"
                    )
                    status = service.attach_resources(target, iso)
            else:
                status = service.eject_resources(target)
        _print_status(status, json_output=args.json)
    return 0


def _print_resources(resources: Sequence[Resource], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps([asdict(resource) for resource in resources], default=str))
    elif resources:
        for resource in resources:
            print(
                f"{resource.id}  {resource.kind}  {resource.name}  ({resource.size_bytes:,} bytes)"
            )
    else:
        print("No matching library files.")


def _dispatch_git(
    args: _Arguments, sessions: SessionService, service: VMService, store: ConfigStore
) -> int:
    direction = args.sync_action
    if direction == "bundle":
        create_bundle(args.repo, args.branch, args.output)
        print(args.output)
    elif direction == "import":
        print(import_bundle(args.repo, args.path, args.branch))
    elif direction == "export-guest":
        with (
            sessions.outside_session(),
            service.offline_disk(_target_vm(args.vm, store)) as disk,
        ):
            output = export_bundle(disk, args.guest_path, args.output)
        print(json.dumps({"path": str(output)}) if args.json else output)
    else:
        with sessions.outside_session():
            print(sync_remote(args.repo, args.remote, args.branch, direction))
    return 0


def _dispatch_tracking(
    args: _Arguments, queue: ActivityQueue, sessions: SessionService
) -> int:
    action = args.tracking_action
    if action == "enable":
        activity = queue.enable(args.projects)
        print(f"Activity enabled for {', '.join(activity.projects)}.")
    elif action == "disable":
        queue.disable()
        print("Activity disabled. Existing events remain queued.")
    elif action == "status":
        activity = queue.status()
        if args.json:
            print(json.dumps(asdict(activity)))
        else:
            print(f"Enabled projects: {', '.join(activity.projects) or 'none'}")
            for heartbeat in activity.queued:
                print(f"{heartbeat.time:g}  {heartbeat.project}  {heartbeat.entity}")
            print(f"Queued events: {len(activity.queued)}")
    elif action == "record":
        queue.record(args.project, args.entity, args.timestamp)
        print("Activity queued locally.")
    elif action == "clear":
        queue.clear()
        print("Queued activity discarded.")
    elif action == "store-key":
        save_api_key(_hidden_input("WakaTime API key: "))
        print("Key stored in the desktop keyring.")
    elif action == "forget-key":
        forget_api_key()
        print("Stored key removed.")
    else:
        with sessions.outside_session():
            activity = queue.status()
            if not any(event.project in activity.projects for event in activity.queued):
                print("No queued activity for enabled projects.")
            else:
                key = (
                    _hidden_input("WakaTime API key: ")
                    if args.prompt_key
                    else read_api_key()
                )
                print(f"Sent {queue.sync(key)} queued events to WakaTime.")
    return 0


def _hidden_input(prompt: str) -> str:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", GetPassWarning)
            return getpass(prompt)
    except (EOFError, GetPassWarning) as error:
        raise TrackingInputError(
            "Run this command in a terminal to enter the API key securely"
        ) from error


def _dispatch_session(
    args: _Arguments, store: ConfigStore, service: SessionService
) -> int:
    session: FocusSession | None
    if args.session_command == "start":
        settings = (
            _load_config(store) if args.vm is None or args.profile is None else None
        )
        target = (
            args.vm
            if args.vm is not None or settings is None
            else settings.application.default_vm
        )
        profile = args.profile or (
            settings.application.default_profile if settings is not None else PROFILE_ID
        )
        if not args.json:
            _print_viewer_controls(cage=args.cage)
        session = service.start(
            target,
            profile,
            args.network,
            duration_minutes=args.duration_minutes,
            cage=args.cage,
        )
    elif args.session_command == "reopen":
        session = service.reopen()
        if not args.json:
            _print_viewer_controls(cage=session.cage)
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


def _print_viewer_controls(*, cage: bool) -> None:
    if cage:
        print(
            "Close the Cage host window, or run focusvm session end from another host terminal.",
            flush=True,
        )
        return
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
        result = asdict(session)
        if not session.cage:
            result.pop("cage")
        if session.deadline is None:
            result.pop("deadline")
        else:
            result["remaining_seconds"] = session.remaining_seconds()
        print(json.dumps(result, default=str))
        return
    print(f"Focus session for {session.vm_name} ({session.vm}) is {session.state}.")
    print(f"Profile {session.profile} uses {session.network} networking.")
    if session.cage:
        print("Viewer mode: Cage.")
    if (remaining := session.remaining_seconds()) is not None:
        print(
            f"Contest timer: {remaining // 60}:{remaining % 60:02} remaining."
            if remaining
            else "Contest timer expired."
        )
    if session.detail:
        print(session.detail)


def _print_output(path: Path, json_output: bool) -> None:
    print(json.dumps({"path": str(path)}) if json_output else path)


def _dispatch_journal(args: _Arguments, journal: Journal) -> int:
    if args.journal_action == "stats":
        stats = journal.stats(args.project)
        if args.json:
            print(json.dumps(asdict(stats)))
        else:
            print(
                f"{stats.sessions} recorded sessions · {stats.duration_minutes} minutes"
            )
            for project in stats.projects:
                print(
                    f"{project.project} · {project.sessions} sessions · {project.duration_minutes} minutes"
                )
        return 0
    entries: tuple[JournalEntry, ...]
    if args.journal_action == "record":
        entries = (
            journal.record(
                args.project,
                args.day,
                args.journal_minutes,
                completed=args.completed,
                problems=args.problems,
                solved=args.solved,
                references=args.references,
                notes=args.notes,
            ),
        )
    elif args.journal_action == "remove":
        entries = (journal.remove(args.entry_id),)
    else:
        entries = journal.list(args.project)
    if args.json:
        print(json.dumps([asdict(entry) for entry in entries]))
    else:
        for entry in entries:
            print(
                f"{entry.id}  {entry.day}  {entry.project}  {entry.duration_minutes} min"
            )
            for label, items in (
                ("Completed", entry.completed),
                ("Problems", entry.problems),
                ("Solved independently", entry.solved),
                ("References", entry.references),
            ):
                for item in items:
                    print(f"  {label} · {item}")
            if entry.notes:
                print(f"  Notes · {entry.notes}")
    return 0


def _dispatch_research(
    args: _Arguments, research: Research, sessions: SessionService
) -> int:
    if args.research_action == "save":
        with sessions.outside_session():
            _print_output(research.save(args.url, args.output), args.json)
        return 0
    hosts = (
        research.hosts()
        if args.research_action == "hosts"
        else research.update_host(args.host, allow=args.research_action == "allow")
    )
    if args.json:
        print(json.dumps(hosts))
    else:
        print("\n".join(hosts) or "No approved research hosts.")
    return 0


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
    print(
        f"Reference disc: {'attached' if status.resource_media_attached else 'empty'}."
    )
