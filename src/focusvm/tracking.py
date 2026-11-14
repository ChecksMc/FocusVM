"""Opt-in, locally queued WakaTime heartbeats."""

import base64
import json
import math
import shutil
import subprocess
from dataclasses import asdict, dataclass, replace
from http.client import HTTPException, HTTPSConnection
from pathlib import Path, PurePosixPath
from time import time
from typing import cast

from .local_state import atomic_write, file_lock, open_owned_file

_HOST = "api.wakatime.com"
_PATH = "/api/v1/users/current/heartbeats"
_QUEUE_LIMIT = 1000


class TrackingInputError(ValueError):
    """The requested activity operation has invalid input."""


class TrackingError(RuntimeError):
    """Activity could not be read or synchronized safely."""


@dataclass(frozen=True, slots=True)
class Heartbeat:
    project: str
    entity: str
    time: float


@dataclass(frozen=True, slots=True)
class Activity:
    projects: tuple[str, ...] = ()
    queued: tuple[Heartbeat, ...] = ()


class ActivityQueue:
    def __init__(self, state_dir: Path) -> None:
        self.path = state_dir / "activity.json"

    def status(self) -> Activity:
        return self._load()

    def enable(self, projects: list[str]) -> Activity:
        selected = tuple(
            dict.fromkeys(_text(name, "project", 120) for name in projects)
        )
        if not selected:
            raise TrackingInputError("Select at least one project")
        with file_lock(self.path):
            activity = replace(self._load(), projects=selected)
            self._save(activity)
            return activity

    def disable(self) -> Activity:
        with file_lock(self.path):
            activity = replace(self._load(), projects=())
            self._save(activity)
            return activity

    def record(
        self, project: str, entity: str, timestamp: float | None = None
    ) -> Heartbeat:
        event = _event(project, entity, time() if timestamp is None else timestamp)
        with file_lock(self.path):
            activity = self._load()
            if event.project not in activity.projects:
                raise TrackingInputError(
                    "Enable activity for this project before recording"
                )
            if event in activity.queued:
                return event
            if len(activity.queued) >= _QUEUE_LIMIT:
                raise TrackingError(
                    "The activity queue is full. Synchronize queued events first."
                )
            self._save(replace(activity, queued=(*activity.queued, event)))
        return event

    def sync(self, api_key: str) -> int:
        key = _api_key(api_key)
        with file_lock(self.path):
            activity = self._load()
            pending = activity.queued
            sent = 0
            for event in pending:
                if event.project not in activity.projects:
                    continue
                _send(event, key)
                activity = replace(
                    activity,
                    queued=tuple(item for item in activity.queued if item != event),
                )
                self._save(activity)
                sent += 1
            return sent

    def clear(self) -> None:
        with file_lock(self.path):
            self._save(replace(self._load(), queued=()))

    def _save(self, activity: Activity) -> None:
        atomic_write(self.path, json.dumps({"version": 1, **asdict(activity)}) + "\n")

    def _load(self) -> Activity:
        try:
            with open(self.path, encoding="utf-8", opener=open_owned_file) as stream:
                raw: object = json.load(stream)
            payload = _object(raw)
            projects, queued = payload.get("projects"), payload.get("queued")
            version = payload.get("version")
            if (
                type(version) is not int
                or version != 1
                or not isinstance(projects, list)
                or not isinstance(queued, list)
            ):
                raise ValueError("Unsupported activity data")
            return Activity(
                tuple(
                    _text(name, "project", 120)
                    for name in cast("list[object]", projects)
                ),
                tuple(_parse_event(item) for item in cast("list[object]", queued)),
            )
        except FileNotFoundError:
            return Activity()
        except (ValueError, RecursionError) as error:
            raise TrackingError(
                f"Could not read activity queue {self.path}: {error}"
            ) from error


def save_api_key(key: str) -> None:
    _secret_tool(
        "store",
        "--label=FocusVM WakaTime",
        "application",
        "focusvm",
        "service",
        "wakatime",
        value=_api_key(key),
    )


def read_api_key() -> str:
    return _api_key(
        _secret_tool("lookup", "application", "focusvm", "service", "wakatime").rstrip(
            "\n"
        )
    )


def forget_api_key() -> None:
    _secret_tool("clear", "application", "focusvm", "service", "wakatime")


def _secret_tool(*args: str, value: str | None = None) -> str:
    tool = shutil.which("secret-tool")
    if tool is None:
        raise TrackingError(
            "Install secret-tool and unlock a desktop Secret Service keyring, or use an interactive API key prompt"
        )
    try:
        result = subprocess.run(  # noqa: S603 - installed tool with fixed arguments.
            [tool, *args],
            input=value,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise TrackingError("The desktop Secret Service did not respond") from error
    if result.returncode:
        raise TrackingError(
            "The desktop Secret Service could not access the FocusVM key"
        )
    return result.stdout


def _send(event: Heartbeat, key: str) -> None:
    authorization = base64.b64encode(key.encode("ascii")).decode("ascii")
    body = json.dumps({**asdict(event), "type": "file", "category": "coding"})
    connection = HTTPSConnection(_HOST, timeout=15)
    try:
        connection.request(
            "POST",
            _PATH,
            body=body,
            headers={
                "Authorization": f"Basic {authorization}",
                "Content-Type": "application/json",
                "User-Agent": "FocusVM",
            },
        )
        with connection.getresponse() as response:
            if response.status not in (201, 202):
                raise TrackingError(
                    f"WakaTime rejected the heartbeat (HTTP {response.status}). The event remains queued."
                )
    except (OSError, HTTPException) as error:
        raise TrackingError(
            "Could not reach WakaTime. Unconfirmed events remain queued."
        ) from error
    finally:
        connection.close()


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("Expected an activity object")
    return cast("dict[str, object]", value)


def _parse_event(value: object) -> Heartbeat:
    event = _object(value)
    return _event(event.get("project"), event.get("entity"), event.get("time"))


def _event(project: object, entity: object, timestamp: object) -> Heartbeat:
    name = _text(project, "project", 120)
    entry = _text(entity, "project-relative file path", 1024)
    path = PurePosixPath(entry)
    if (
        path.is_absolute()
        or not path.parts
        or ".." in path.parts
        or "\\" in entry
        or ":" in entry
    ):
        raise TrackingInputError("Use a project-relative file path inside the project")
    if (
        isinstance(timestamp, bool)
        or not isinstance(timestamp, (int, float))
        or not 0 < timestamp <= time() + 60
        or not math.isfinite(timestamp)
    ):
        raise TrackingInputError(
            "Use a valid Unix timestamp no later than the current time"
        )
    return Heartbeat(name, path.as_posix(), float(timestamp))


def _text(value: object, label: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or not value.isprintable()
        or len(value) > maximum
    ):
        raise TrackingInputError(
            f"Use a nonempty {label} of up to {maximum} printable characters"
        )
    return value


def _api_key(value: str) -> str:
    if (
        not 1 <= len(value) <= 256
        or not value.isascii()
        or not all(33 <= ord(char) <= 126 for char in value)
    ):
        raise TrackingInputError("Enter a valid WakaTime API key")
    return value
