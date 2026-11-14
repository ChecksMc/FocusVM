"""Durable, voluntary offline focus sessions shared by the CLI and manager."""

import json
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast
from uuid import UUID, uuid4

from .local_state import atomic_write, file_lock, open_owned_file
from .viewer import (
    ViewerError,
    check_focus_viewer,
    close_focus_viewer,
    focus_viewer_running,
    launch_focus_viewer,
)
from .virtualization import VirtualizationError, VirtualizationUnavailable
from .vm import VMInputError, VMService, VMStateError

PROFILE_ID = "deep-work"
PROFILE_NAME = "Deep Work"
DEFAULT_NETWORK = "balanced"
OFFLINE_NETWORKS = ("balanced", "strict")

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


@dataclass(frozen=True, slots=True)
class _Intent:
    token: UUID
    vm: UUID
    vm_name: str
    network: str
    started_at: str

    def view(self, state: SessionState, detail: str | None = None) -> FocusSession:
        return FocusSession(
            self.vm,
            self.vm_name,
            PROFILE_ID,
            self.network,
            state,
            self.started_at,
            detail,
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
        network: str = DEFAULT_NETWORK,
    ) -> FocusSession:
        if profile != PROFILE_ID:
            raise SessionInputError(
                f"Unknown focus profile {profile!r}. Use {PROFILE_ID}."
            )
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
            command = check_focus_viewer(vm.uuid)
            intent = _Intent(
                uuid4(), vm.uuid, vm.name, network, datetime.now(UTC).isoformat()
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
            command = check_focus_viewer(intent.vm)
            close_focus_viewer(intent.token)
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
            close_focus_viewer(intent.token)
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
        launch_focus_viewer(command, intent.token, self.log)
        return intent.view("active")

    def _snapshot(self, intent: _Intent) -> FocusSession:
        try:
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

    def _load(self) -> _Intent | None:
        try:
            with open(self.path, encoding="utf-8", opener=open_owned_file) as stream:
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
            return _Intent(UUID(token), UUID(vm), vm_name, network, started)
        except FileNotFoundError:
            return None
        except ValueError as error:
            raise SessionError(
                f"Could not read focus session {self.path}: {error}"
            ) from error


def _field(record: dict[str, object], field: str) -> str:
    value = record.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Invalid focus-session field {field}")
    return value
