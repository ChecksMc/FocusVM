import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from threading import Barrier
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest

from focusvm.sessions import SessionError, SessionInputError, SessionService
from focusvm.virtualization import (
    VirtualizationError,
    VirtualizationUnavailable,
    VMStatus,
)
from focusvm.vm import VMService


def _vm(vm: UUID, *, active: bool) -> VMStatus:
    return VMStatus(
        vm,
        "development",
        "running" if active else "shut-off",
        active,
        2,
        4096,
        2**35,
        1024,
    )


def test_session_survives_frontend_restart_and_ends_without_stopping_vm(
    tmp_path: Path,
) -> None:
    vm_id = uuid4()
    vm_service = VMService(tmp_path, tmp_path)
    first = SessionService(tmp_path, vm_service)
    command = ("virt-viewer", [str(vm_id)])
    with (
        patch.object(
            vm_service,
            "focus_status",
            side_effect=[_vm(vm_id, active=False), _vm(vm_id, active=True)],
        ),
        patch.object(
            vm_service, "start", return_value=_vm(vm_id, active=True)
        ) as start,
        patch.object(vm_service, "stop") as stop,
        patch.object(vm_service, "check_console") as check,
        patch("focusvm.sessions.check_focus_viewer", return_value=command),
        patch("focusvm.sessions.launch_focus_viewer") as launch,
        patch("focusvm.sessions.focus_viewer_running", return_value=True) as running,
        patch("focusvm.sessions.close_focus_viewer") as close,
    ):
        session = first.start(None, network="strict")
        second = SessionService(tmp_path, vm_service)
        assert second.status() == session
        assert first.path.stat().st_mode & 0o777 == 0o600
        assert session.network == "strict"
        token = running.call_args.args[0]
        start.assert_called_once_with(vm_id)
        check.assert_called_once_with(vm_id)
        launch.assert_called_once_with(command, token, first.log)
        with pytest.raises(SessionInputError, match="already exists"):
            second.start(vm_id)
        second.end()
        second.end()
        close.assert_called_once_with(token)
        stop.assert_not_called()
        assert first.status() is None


def test_interrupted_viewer_launch_can_be_reopened(tmp_path: Path) -> None:
    vm_id = uuid4()
    vm_service = VMService(tmp_path, tmp_path)
    session = SessionService(tmp_path, vm_service)
    with (
        patch.object(vm_service, "focus_status", return_value=_vm(vm_id, active=True)),
        patch.object(vm_service, "start", return_value=_vm(vm_id, active=True)),
        patch.object(vm_service, "check_console"),
        patch("focusvm.sessions.check_focus_viewer", return_value=("virt-viewer", [])),
        patch(
            "focusvm.sessions.launch_focus_viewer",
            side_effect=[OSError("viewer failure"), None],
        ) as launch,
        patch("focusvm.sessions.focus_viewer_running", return_value=False),
        patch("focusvm.sessions.close_focus_viewer"),
    ):
        with pytest.raises(OSError, match="viewer failure"):
            session.start(vm_id)
        disconnected = session.status()
        assert disconnected is not None and disconnected.state == "disconnected"
        active = session.reopen()
        assert active.started_at == disconnected.started_at
        assert active.state == "active"
        assert launch.call_args_list[0].args[1] == launch.call_args_list[1].args[1]


def test_policy_drift_blocks_reopen_but_allows_local_end(tmp_path: Path) -> None:
    vm_id = uuid4()
    vm_service = VMService(tmp_path, tmp_path)
    session = SessionService(tmp_path, vm_service)
    drift = VirtualizationError("Unexpected guest network device")
    with (
        patch.object(
            vm_service,
            "focus_status",
            side_effect=[_vm(vm_id, active=True), drift, drift],
        ),
        patch.object(vm_service, "start", return_value=_vm(vm_id, active=True)),
        patch.object(vm_service, "check_console"),
        patch("focusvm.sessions.check_focus_viewer", return_value=("virt-viewer", [])),
        patch("focusvm.sessions.launch_focus_viewer"),
        patch("focusvm.sessions.focus_viewer_running", return_value=True),
        patch("focusvm.sessions.close_focus_viewer") as close,
    ):
        session.start(vm_id)
        current = session.status()
        assert current is not None and current.state == "unresolved"
        with pytest.raises(SessionError, match="Unexpected guest network device"):
            session.reopen()
        session.end()
        close.assert_called_once()
        assert session.status() is None


def test_flexible_and_unsafe_vm_fail_before_persisting_intent(tmp_path: Path) -> None:
    vm_service = VMService(tmp_path, tmp_path)
    session = SessionService(tmp_path, vm_service)
    with pytest.raises(VirtualizationUnavailable, match="Flexible"):
        session.start(None, network="flexible")
    with patch.object(
        vm_service, "focus_status", side_effect=VirtualizationError("Policy mismatch")
    ):
        with pytest.raises(VirtualizationError, match="Policy mismatch"):
            session.start(None)
    assert session.status() is None


def test_contest_deadline_survives_reopen_and_can_expire_without_ending(
    tmp_path: Path,
) -> None:
    vm_id = uuid4()
    vm_service = VMService(tmp_path, tmp_path)
    sessions = SessionService(tmp_path, vm_service)
    with (
        patch.object(vm_service, "focus_status", return_value=_vm(vm_id, active=True)),
        patch.object(vm_service, "start"),
        patch.object(vm_service, "check_console"),
        patch("focusvm.sessions.check_focus_viewer", return_value=("virt-viewer", [])),
        patch("focusvm.sessions.launch_focus_viewer"),
        patch("focusvm.sessions.focus_viewer_running", return_value=False),
        patch("focusvm.sessions.close_focus_viewer"),
    ):
        with pytest.raises(SessionInputError, match="Contest timer"):
            sessions.start(vm_id, profile="research", duration_minutes=25)
        contest = sessions.start(vm_id, profile="contest", duration_minutes=25)
        assert contest.network == "strict"
        assert contest.deadline is not None
        assert datetime.fromisoformat(contest.deadline) - datetime.fromisoformat(
            contest.started_at
        ) == timedelta(minutes=25)
        restored = SessionService(tmp_path, vm_service).reopen()
        assert (restored.profile, restored.deadline) == (
            contest.profile,
            contest.deadline,
        )
        assert replace(restored, deadline=restored.started_at).remaining_seconds() == 0
        assert sessions.status() is not None


def test_phase_six_intent_recovers_as_deep_work(tmp_path: Path) -> None:
    vm_id = uuid4()
    vm_service = VMService(tmp_path, tmp_path)
    sessions = SessionService(tmp_path, vm_service)
    sessions.path.write_text(
        json.dumps(
            {
                "version": 1,
                "token": str(uuid4()),
                "vm": str(vm_id),
                "vm_name": "development",
                "network": "balanced",
                "started_at": "2026-11-14T13:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    with (
        patch.object(vm_service, "focus_status", return_value=_vm(vm_id, active=True)),
        patch("focusvm.sessions.focus_viewer_running", return_value=True),
    ):
        recovered = sessions.status()
    assert recovered is not None
    assert recovered.profile == "deep-work"
    assert recovered.deadline is None
    assert not recovered.cage


def test_concurrent_starts_launch_one_viewer(tmp_path: Path) -> None:
    vm_id = uuid4()
    vm_service = VMService(tmp_path, tmp_path)
    session = SessionService(tmp_path, vm_service)
    gate = Barrier(2, timeout=5)

    def attempt() -> bool:
        gate.wait()
        try:
            session.start(vm_id)
        except SessionInputError:
            return False
        return True

    with (
        patch.object(vm_service, "focus_status", return_value=_vm(vm_id, active=True)),
        patch.object(vm_service, "start", return_value=_vm(vm_id, active=True)),
        patch.object(vm_service, "check_console"),
        patch("focusvm.sessions.check_focus_viewer", return_value=("virt-viewer", [])),
        patch("focusvm.sessions.launch_focus_viewer") as launch,
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        first = pool.submit(attempt)
        second = pool.submit(attempt)
        assert sorted([first.result(), second.result()]) == [False, True]
        launch.assert_called_once()
