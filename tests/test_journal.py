import json
from datetime import UTC, date, datetime
from pathlib import Path
from stat import S_IMODE
from uuid import uuid4

import pytest

from focusvm.journal import Journal, JournalError, JournalInputError, JournalStats


def test_journal_records_only_explicit_work_in_private_state(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "state")
    assert journal.list() == ()
    assert journal.stats() == JournalStats(0, 0, ())
    assert not journal.path.parent.exists()

    entry = journal.record(
        "Numerical methods",
        date(2026, 11, 12),
        75,
        completed=["Implemented interpolation"],
        problems=["Unexpected rounding"],
        solved=["Boundary cases"],
        references=["C++ reference"],
        notes="First attempt\nRetry with a smaller step",
    )

    assert S_IMODE(journal.path.stat().st_mode) == 0o600
    assert Journal(journal.path.parent).list() == (entry,)
    assert entry.day == "2026-11-12"
    assert entry.completed == ("Implemented interpolation",)
    assert entry.problems == ("Unexpected rounding",)
    assert entry.solved == ("Boundary cases",)
    assert entry.references == ("C++ reference",)
    assert entry.notes == "First attempt\nRetry with a smaller step"
    assert journal.remove(entry.id) == entry
    assert journal.list() == ()


def test_journal_filters_and_aggregates_minutes_per_project(tmp_path: Path) -> None:
    journal = Journal(tmp_path)
    research = journal.record("Research", date(2026, 11, 12), 25)
    contest = journal.record("Contest", date(2026, 11, 11), 45)
    second = journal.record("Research", date(2026, 11, 13), 30)
    assert journal.list() == (second, research, contest)
    assert journal.list("Research") == (second, research)
    stats = journal.stats()
    assert (stats.sessions, stats.duration_minutes) == (3, 100)
    assert [
        (project.project, project.sessions, project.duration_minutes)
        for project in stats.projects
    ] == [("Contest", 1, 45), ("Research", 2, 55)]
    assert journal.stats("Research").duration_minutes == 55
    assert journal.stats("Other") == JournalStats(0, 0, ())


def test_journal_rejects_invalid_input_without_writing(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "state")
    with pytest.raises(JournalInputError, match="project name"):
        journal.record("  ", date.today(), 1)
    with pytest.raises(JournalInputError, match="positive whole number"):
        journal.record("Work", date.today(), False)
    with pytest.raises(JournalInputError, match="readable text for references"):
        journal.record("Work", date.today(), 1, references=["bad\x1b[0m"])
    with pytest.raises(JournalInputError, match="journal entry ID"):
        journal.remove("../entry")
    assert not journal.path.parent.exists()


def test_timed_entries_preserve_seconds_and_allow_retries(tmp_path: Path) -> None:
    journal = Journal(tmp_path)
    ended = datetime(2026, 11, 12, 12, tzinfo=UTC)
    token = uuid4()
    manual = journal.record("Work", ended.date(), 25)
    journal.record_session(token, "Work", ended, 20, note="")
    journal.record_session(token, "Work", ended, 30, note="Duplicate")
    journal.record_session(uuid4(), "Work", ended, 45, note="Finished")

    timed = [entry for entry in journal.list() if entry.duration_seconds is not None]
    assert [(entry.duration_seconds, entry.notes) for entry in timed] == [
        (45, "Finished"),
        (20, ""),
    ]
    assert "duration_seconds" not in manual.to_record()
    assert journal.stats().duration_minutes == 26


def test_journal_preserves_corrupt_state_on_attempted_record(tmp_path: Path) -> None:
    journal = Journal(tmp_path)
    entry = journal.record("Work", date.today(), 1)
    payload = json.loads(journal.path.read_text())
    payload["entries"].append(payload["entries"][0])
    corrupt = json.dumps(payload)
    journal.path.write_text(corrupt)
    with pytest.raises(JournalError, match="Duplicate journal entry ID"):
        journal.record(entry.project, date.today(), 30)
    assert journal.path.read_text() == corrupt


def test_journal_rejects_symlinked_state(tmp_path: Path) -> None:
    journal = Journal(tmp_path)
    target = tmp_path / "secret.json"
    target.write_text("private")
    journal.path.symlink_to(target)
    with pytest.raises(JournalError, match="Could not read journal"):
        journal.record("Work", date.today(), 30)
    assert target.read_text() == "private"
    assert journal.path.is_symlink()
