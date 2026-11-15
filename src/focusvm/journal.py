"""Private session history and summaries."""

import json
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

from .local_state import atomic_write, file_lock, open_private_file


class JournalInputError(ValueError):
    """A journal command contains invalid input."""


class JournalError(RuntimeError):
    """The saved journal could not be read."""


@dataclass(frozen=True, slots=True)
class JournalEntry:
    id: str
    project: str
    day: str
    duration_minutes: int
    completed: tuple[str, ...]
    problems: tuple[str, ...]
    solved: tuple[str, ...]
    references: tuple[str, ...]
    notes: str
    duration_seconds: int | None = None

    def to_record(self) -> dict[str, object]:
        record = asdict(self)
        if self.duration_seconds is None:
            del record["duration_seconds"]
        return record


@dataclass(frozen=True, slots=True)
class ProjectStats:
    project: str
    sessions: int
    duration_minutes: int


@dataclass(frozen=True, slots=True)
class JournalStats:
    sessions: int
    duration_minutes: int
    projects: tuple[ProjectStats, ...]


class Journal:
    def __init__(self, state_dir: Path) -> None:
        self.path = state_dir / "journal.json"

    def record(
        self,
        project: str,
        day: date,
        duration_minutes: int,
        *,
        completed: Sequence[str] = (),
        problems: Sequence[str] = (),
        solved: Sequence[str] = (),
        references: Sequence[str] = (),
        notes: str = "",
    ) -> JournalEntry:
        entry = JournalEntry(
            id=uuid4().hex,
            project=_project(project),
            day=_day(day),
            duration_minutes=_duration(duration_minutes),
            completed=_items(completed, "completed tasks"),
            problems=_items(problems, "problems encountered"),
            solved=_items(solved, "independently solved problems"),
            references=_items(references, "references"),
            notes=validate_note(notes),
        )
        with file_lock(self.path):
            self._save((*self._load(), entry))
        return entry

    def record_session(
        self, token: UUID, project: str, ended: datetime, seconds: int, *, note: str
    ) -> None:
        seconds = _seconds(seconds)
        entry = JournalEntry(
            id=token.hex,
            project=_project(project),
            day=ended.astimezone().date().isoformat(),
            duration_minutes=seconds // 60,
            completed=(),
            problems=(),
            solved=(),
            references=(),
            notes=validate_note(note),
            duration_seconds=seconds,
        )
        with file_lock(self.path):
            entries = self._load()
            if all(previous.id != entry.id for previous in entries):
                self._save((*entries, entry))

    def list(self, project: str | None = None) -> tuple[JournalEntry, ...]:
        selected = _project(project) if project is not None else None
        entries = (
            entry
            for entry in reversed(self._load())
            if selected is None or selected == entry.project
        )
        return tuple(sorted(entries, key=lambda entry: entry.day, reverse=True))

    def remove(self, entry_id: str) -> JournalEntry:
        entry_id = _identifier(entry_id)
        with file_lock(self.path):
            entries = self._load()
            entry = next((item for item in entries if item.id == entry_id), None)
            if entry is None:
                raise JournalInputError(f"No journal entry with ID {entry_id}")
            self._save(tuple(item for item in entries if item.id != entry_id))
        return entry

    def stats(self, project: str | None = None) -> JournalStats:
        entries = self.list(project)
        sessions = Counter(entry.project for entry in entries)
        seconds: Counter[str] = Counter()
        for entry in entries:
            elapsed = entry.duration_seconds
            seconds[entry.project] += (
                entry.duration_minutes * 60 if elapsed is None else elapsed
            )
        projects = tuple(
            ProjectStats(name, sessions[name], seconds[name] // 60)
            for name in sorted(sessions, key=lambda name: (name.casefold(), name))
        )
        return JournalStats(len(entries), seconds.total() // 60, projects)

    def _save(self, entries: tuple[JournalEntry, ...]) -> None:
        payload = {"version": 1, "entries": [entry.to_record() for entry in entries]}
        atomic_write(self.path, json.dumps(payload, ensure_ascii=False) + "\n")

    def _load(self) -> tuple[JournalEntry, ...]:
        try:
            with open(self.path, encoding="utf-8", opener=open_private_file) as stream:
                raw: object = json.load(stream)
            payload = _object(raw)
            version, entries = payload.get("version"), payload.get("entries")
            if (
                type(version) is not int
                or version != 1
                or not isinstance(entries, list)
            ):
                raise ValueError("Unsupported journal format")
            decoded = tuple(_read_entry(item) for item in cast("list[object]", entries))
            if len({entry.id for entry in decoded}) != len(decoded):
                raise ValueError("Duplicate journal entry ID")
        except FileNotFoundError:
            return ()
        except (OSError, ValueError, RecursionError) as error:
            raise JournalError(
                f"Could not read journal {self.path}: {error}"
            ) from error
        return decoded


def _read_entry(value: object) -> JournalEntry:
    entry = _object(value)
    seconds = (
        _seconds(entry["duration_seconds"]) if "duration_seconds" in entry else None
    )
    minutes = entry.get("duration_minutes")
    if seconds is None:
        minutes = _duration(minutes)
    elif type(minutes) is not int or minutes != seconds // 60:
        raise JournalInputError("Journal minutes disagree with the session duration")
    return JournalEntry(
        id=_identifier(entry.get("id")),
        project=_project(entry.get("project")),
        day=_day(entry.get("day")),
        duration_minutes=minutes,
        completed=_items(entry.get("completed"), "completed tasks"),
        problems=_items(entry.get("problems"), "problems encountered"),
        solved=_items(entry.get("solved"), "independently solved problems"),
        references=_items(entry.get("references"), "references"),
        notes=validate_note(entry.get("notes")),
        duration_seconds=seconds,
    )


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("Expected a journal object")
    return cast("dict[str, object]", value)


def _identifier(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{32}", value) is None:
        raise JournalInputError("Use a journal entry ID from the journal list")
    return value


def _project(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or not value.isprintable():
        raise JournalInputError("Enter a project name on one line")
    return value.strip()


def _day(value: object) -> str:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str):
        try:
            if date.fromisoformat(value).isoformat() == value:
                return value
        except ValueError:
            pass
    raise JournalInputError("Use a journal date in YYYY-MM-DD format")


def _duration(value: object) -> int:
    if type(value) is not int or value < 1:
        raise JournalInputError("Enter a positive whole number of minutes")
    return value


def _seconds(value: object) -> int:
    if type(value) is not int or value < 0:
        raise JournalInputError("Use a nonnegative session duration in seconds")
    return value


def _items(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise JournalInputError(f"Use a list for {label}")
    items: list[str] = []
    for item in cast("Sequence[object]", value):
        if not isinstance(item, str) or not item.strip() or not _readable(item):
            raise JournalInputError(f"Enter readable text for {label}")
        items.append(item)
    return tuple(items)


def validate_note(value: object) -> str:
    if not isinstance(value, str) or not _readable(value):
        raise JournalInputError("Enter readable text for notes")
    return value


def _readable(value: str) -> bool:
    return all(character.isprintable() or character in "\n\t" for character in value)
