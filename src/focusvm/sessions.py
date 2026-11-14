"""Durable, voluntary offline focus sessions shared by the CLI and manager."""

import json
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, cast
from uuid import UUID, uuid4

from .local_state import atomic_write, file_lock, open_private_file
from .profiles import PROFILES, ProfileId
from .viewer import (
    ViewerError,
    check_focus_viewer,
    close_focus_viewer,
    focus_viewer_running,
    launch_focus_viewer,
)
from .virtualization import VirtualizationError, VirtualizationUnavailable
from .vm import VMInputError, VMService, VMStateError

PROFILE_ID: Literal["deep-work"] = "deep-work"
PROFILE_NAME = PROFILES[PROFILE_ID].name
DEFAULT_NETWORK = PROFILES[PROFILE_ID].network
OFFLINE_NETWORKS = ("balanced", "strict")
MAX_CONTEST_MINUTES = 24 * 60

type SessionState = Literal["active", "disconnected", "unresolved"]


class SessionInputError(ValueError):
    """A session action or requested profile is invalid."""


class SessionError(RuntimeError):
    """The current focus session could not be read or reconciled."""


@dataclass(frozen=True, slots=True)
class FocusSession:
    vm: UUID
    vm_name: str
    profile: str
    network: str
    state: SessionState
    started_at: str
    detail: str | None = None
    deadline: str | None = None
    cage: bool = False

    def remaining_seconds(self) -> int | None:
        if self.deadline is None:
            return None
        remaining = datetime.fromisoformat(self.deadline) - datetime.now(UTC)
        return max(0, -(-remaining // timedelta(seconds=1)))


@dataclass(frozen=True, slots=True)
class _Intent:
    token: UUID
    vm: UUID
    vm_name: str
    network: str
    started_at: str
    profile: ProfileId = PROFILE_ID
    duration_minutes: int | None = None
    cage: bool = False

    def view(self, state: SessionState, detail: str | None = None) -> FocusSession:
        deadline = None
        if self.duration_minutes is not None:
            deadline = (
                datetime.fromisoformat(self.started_at)
                + timedelta(minutes=self.duration_minutes)
            ).isoformat()
        return FocusSession(
            self.vm,
            self.vm_name,
            self.profile,
            self.network,
            state,
            self.started_at,
            detail,
            deadline,
            self.cage,
        )


class SessionService:
    def __init__(self, state_dir: Path, vm_service: VMService) -> None:
        self.path = state_dir / "focus-session.json"
        self.log = state_dir / "focus-viewer.log"
        self.vm_service = vm_service

    def start(
        self,
        target: UUID | None,
        profile: str = PROFILE_ID,
        network: str | None = None,
        *,
        duration_minutes: int | None = None,
        cage: bool = False,
    ) -> FocusSession:
        selected = _profile(profile)
        network = PROFILES[selected].network if network is None else network
        _contest_duration(selected, duration_minutes)
        if network == "flexible":
            raise VirtualizationUnavailable(
                "Flexible networking is not available for focus sessions"
            )
        if network not in OFFLINE_NETWORKS:
            raise SessionInputError(f"Unknown focus network policy {network!r}")

        with file_lock(self.path):
            if self._load() is not None:
                raise SessionInputError(
                    "A focus session already exists. Reopen or end it first."
                )
            vm = self.vm_service.focus_status(target)
            command = self._viewer_command(vm.uuid, cage)
            intent = _Intent(
                uuid4(),
                vm.uuid,
                vm.name,
                network,
                datetime.now(UTC).isoformat(),
                selected,
                duration_minutes,
                cage,
            )
            self._save(intent)
            return self._launch(intent, command)

    def reopen(self) -> FocusSession:
        with file_lock(self.path):
            intent = self._load()
            if intent is None:
                raise SessionInputError("There is no focus session to reopen")
            current = self._snapshot(intent)
            if current.state == "active":
                return current
            if current.state == "unresolved":
                raise SessionError(
                    current.detail or "The focus session could not be verified"
                )
            command = self._viewer_command(intent.vm, intent.cage)
            self._close_viewer(intent)
            return self._launch(intent, command)

    def status(self) -> FocusSession | None:
        with file_lock(self.path):
            intent = self._load()
            return None if intent is None else self._snapshot(intent)

    def end(self) -> None:
        with file_lock(self.path):
            intent = self._load()
            if intent is None:
                return
            self._close_viewer(intent)
            self._save(None)

    @contextmanager
    def outside_session(self) -> Generator[None]:
        """Exclude a focus session for the lifetime of a host maintenance action."""
        with file_lock(self.path):
            if self._load() is not None:
                raise SessionInputError(
                    "End the focus session before performing this operation"
                )
            yield

    def _launch(self, intent: _Intent, command: tuple[str, list[str]]) -> FocusSession:
        # VMService refuses unrecognized devices, shares, or external networking before
        # resuming the guest. Recheck the live console before creating a host viewer.
        self.vm_service.start(intent.vm)
        self.vm_service.check_console(intent.vm)
        if intent.cage:
            from .cage import launch_cage_viewer

            launch_cage_viewer(command, intent.token, self.log)
        else:
            launch_focus_viewer(command, intent.token, self.log)
        return intent.view("active")

    def _snapshot(self, intent: _Intent) -> FocusSession:
        try:
            if intent.cage:
                from .cage import cage_running

                viewer_open = cage_running(intent.token)
            else:
                viewer_open = focus_viewer_running(intent.token)
            vm = self.vm_service.focus_status(intent.vm)
        except (
            OSError,
            ViewerError,
            VMInputError,
            VMStateError,
            VirtualizationError,
        ) as error:
            return intent.view("unresolved", str(error))

        if viewer_open and vm.active:
            return intent.view("active")
        if not vm.active:
            return intent.view(
                "disconnected",
                "The guest is stopped. Reopen the session to boot it offline.",
            )
        return intent.view(
            "disconnected",
            f"The viewer is closed. The guest is still running offline. Viewer log: {self.log}",
        )

    def _save(self, intent: _Intent | None) -> None:
        record = None if intent is None else {"version": 1, **asdict(intent)}
        atomic_write(self.path, json.dumps(record, default=str) + "\n")

    def _viewer_command(self, vm: UUID, cage: bool) -> tuple[str, list[str]]:
        if cage:
            from .cage import check_cage_viewer

            return check_cage_viewer(vm)
        return check_focus_viewer(vm)

    def _close_viewer(self, intent: _Intent) -> None:
        if intent.cage:
            from .cage import close_cage

            close_cage(intent.token)
        else:
            close_focus_viewer(intent.token)

    def _load(self) -> _Intent | None:
        try:
            with open(self.path, encoding="utf-8", opener=open_private_file) as stream:
                record: object = json.load(stream)
            if record is None:
                return None
            if not isinstance(record, dict):
                raise ValueError("Expected a focus-session record")
            data = cast("dict[str, object]", record)
            if type(data.get("version")) is not int or data["version"] != 1:
                raise ValueError("Unsupported focus-session record version")
            token, vm, vm_name, network, started = (
                _field(data, key)
                for key in ("token", "vm", "vm_name", "network", "started_at")
            )
            if (
                network not in OFFLINE_NETWORKS
                or datetime.fromisoformat(started).utcoffset() is None
            ):
                raise ValueError("Invalid focus-session network or start time")
            profile = _profile(data.get("profile", PROFILE_ID))
            duration = _contest_duration(profile, data.get("duration_minutes"))
            cage = data.get("cage", False)
            if not isinstance(cage, bool):
                raise ValueError("Invalid focus-session viewer mode")
            intent = _Intent(
                UUID(token),
                UUID(vm),
                vm_name,
                network,
                started,
                profile,
                duration,
                cage,
            )
            if duration is not None:
                intent.view("disconnected")
            return intent
        except FileNotFoundError:
            return None
        except (ValueError, OverflowError) as error:
            raise SessionError(
                f"Could not read focus session {self.path}: {error}"
            ) from error


def _field(record: dict[str, object], field: str) -> str:
    value = record.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Invalid focus-session field {field}")
    return value


def _profile(value: object) -> ProfileId:
    for profile in PROFILES:
        if value == profile:
            return profile
    raise SessionInputError(f"Choose a focus profile from {', '.join(PROFILES)}")


def _contest_duration(profile: ProfileId, value: object) -> int | None:
    if value is None:
        return None
    if (
        profile != "contest"
        or type(value) is not int
        or not 1 <= value <= MAX_CONTEST_MINUTES
    ):
        raise SessionInputError(
            f"Choose 1 to {MAX_CONTEST_MINUTES} minutes for a Contest timer"
        )
    return value
