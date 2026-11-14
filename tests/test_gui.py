# These tests observe callbacks behind the window's Qt event-handling boundary.
# pyright: reportPrivateUsage=false

from dataclasses import replace
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from threading import Event
from uuid import UUID, uuid4

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QListWidget,
    QPlainTextEdit,
    QPushButton,
    QWidget,
)
from pytestqt.qtbot import QtBot

from focusvm.config import ConfigPaths, ConfigStore, Settings, VMDefaults
from focusvm.diagnostics import Diagnostic
from focusvm.gui import window as gui_window
from focusvm.gui.dialogs import CreateVMDialog
from focusvm.provisioning import ImageInput
from focusvm.sessions import PROFILE_ID, FocusSession, SessionError
from focusvm.virtualization import VirtualizationError, VMStatus
from focusvm.vm import GIB, VMRecord

VM_ID = UUID("e5cdd24a-92bd-4375-af35-822a085975df")
VM_NAME = "test-vm"
type Desktop = tuple[gui_window.MainWindow, ConfigStore, _VMService]


class _VMService:
    def __init__(self) -> None:
        self.entered_start = Event()
        self.finish_start = Event()
        self.start_error: str | None = None
        self.active = False
        self.created: tuple[str, ImageInput, VMDefaults] | None = None
        self.guest_secret = uuid4().hex

    def list_records(self) -> tuple[VMRecord, ...]:
        return (
            VMRecord(VM_ID, VM_NAME, "focusvm", "a" * 64, VMDefaults(), phase="ready"),
        )

    def status(self, target: UUID | None) -> VMStatus:
        assert target == VM_ID
        return VMStatus(
            uuid=VM_ID,
            name=VM_NAME,
            state="running" if self.active else "shut off",
            active=self.active,
            cpus=2,
            memory_mib=4096,
            disk_capacity_bytes=32 * GIB,
            disk_allocation_bytes=GIB,
        )

    def start(self, target: UUID | None) -> VMStatus:
        self.entered_start.set()
        if not self.finish_start.wait(timeout=3):
            raise VirtualizationError("Test did not release pending start")
        if self.start_error is not None:
            raise VirtualizationError(self.start_error)
        self.active = True
        return self.status(target)

    def stop(self, target: UUID | None, timeout: int) -> VMStatus:
        assert timeout == 60
        self.active = False
        return self.status(target)

    def create(
        self,
        name: str,
        source: ImageInput,
        password: str,
        pool: str,
        resources: VMDefaults,
    ) -> VMStatus:
        assert pool == "focusvm"
        assert password == self.guest_secret
        self.created = (name, source, resources)
        return self.status(VM_ID)


def _widget[W: QWidget](parent: QWidget, cls: type[W], name: str) -> W:
    widget = parent.findChild(cls, name)
    assert widget is not None, name
    return widget


@pytest.fixture
def desktop(qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Desktop:
    paths = ConfigPaths(
        config_dir=tmp_path / "config",
        state_dir=tmp_path / "state",
        cache_dir=tmp_path / "cache",
        data_dir=tmp_path / "data",
    )
    store = ConfigStore(paths.config_file)
    settings = Settings.for_pool("focusvm")
    store.initialize(
        replace(settings, application=replace(settings.application, default_vm=VM_ID))
    )
    service = _VMService()

    def use_service(state_dir: Path, cache_dir: Path) -> _VMService:
        assert (state_dir, cache_dir) == (paths.state_dir, paths.cache_dir)
        return service

    monkeypatch.setattr(gui_window, "VMService", use_service)
    window = gui_window.MainWindow(paths, show_wizard=False)
    qtbot.addWidget(window)
    window.show()
    return window, store, service


def _go_to(window: QWidget, page: str) -> None:
    navigation = _widget(window, QListWidget, "navigation")
    for index in range(navigation.count()):
        if navigation.item(index).text() == page:
            navigation.setCurrentRow(index)
            return
    pytest.fail(f"Missing navigation page {page!r}")


def test_manager_loads_managed_vm_and_only_implemented_pages(
    qtbot: QtBot, desktop: Desktop
) -> None:
    window, _, _ = desktop
    navigation = _widget(window, QListWidget, "navigation")
    assert [navigation.item(i).text() for i in range(navigation.count())] == [
        "Dashboard",
        "Virtual Machines",
        "Settings",
        "Focus Sessions",
        "Resources",
    ]
    _go_to(window, "Virtual Machines")
    vm_list = _widget(window, QListWidget, "vmList")
    qtbot.waitUntil(lambda: vm_list.count() == 1)
    assert VM_NAME in vm_list.item(0).text()
    vm_list.setCurrentRow(0)
    start = _widget(window, QPushButton, "startButton")
    qtbot.waitUntil(start.isEnabled)
    assert not _widget(window, QPushButton, "stopButton").isEnabled()
    assert not _widget(window, QPushButton, "openButton").isEnabled()


def test_manager_saves_shared_settings(qtbot: QtBot, desktop: Desktop) -> None:
    window, store, _ = desktop
    _go_to(window, "Settings")
    pool = _widget(window, QLineEdit, "storagePool")
    theme = _widget(window, QComboBox, "theme")
    cpus = _widget(window, QLineEdit, "defaultCPUs")
    memory = _widget(window, QLineEdit, "defaultMemory")
    disk = _widget(window, QLineEdit, "defaultDisk")
    save = _widget(window, QPushButton, "settingsSaveButton")
    qtbot.waitUntil(save.isEnabled)
    assert pool.text() == "focusvm"
    assert (cpus.text(), memory.text(), disk.text()) == ("2", "4096", "32")
    pool.setText("focus-pool")
    theme.setCurrentIndex(theme.findText("Dark"))
    cpus.setText("4")
    memory.setText("6144")
    disk.setText("64")
    save.click()
    qtbot.waitUntil(lambda: store.load().application.storage_pool == "focus-pool")
    settings = store.load()
    assert settings.application.theme == "dark"
    assert settings.application.default_vm == VM_ID
    assert settings.vm_defaults == VMDefaults(4, 6144, 64)


def test_host_checks_continue_with_invalid_settings(
    qtbot: QtBot, desktop: Desktop, monkeypatch: pytest.MonkeyPatch
) -> None:
    window, store, _ = desktop
    check = _widget(window, QPushButton, "hostCheckButton")
    results = _widget(window, QPlainTextEdit, "hostResults")
    qtbot.waitUntil(lambda: window.settings is not None and check.isEnabled())
    store.path.write_text("invalid = [", encoding="utf-8")

    def host_checks(paths: ConfigPaths, pool: str | None) -> list[Diagnostic]:
        assert paths == window.paths
        assert pool is None
        return [Diagnostic("kvm", "ok", "KVM available")]

    monkeypatch.setattr("focusvm.diagnostics.diagnose", host_checks)
    check.click()
    qtbot.waitUntil(lambda: "KVM available" in results.toPlainText())
    assert "ERROR  Configuration" in results.toPlainText()


def test_manager_runs_vm_actions_without_blocking_navigation(
    qtbot: QtBot, desktop: Desktop
) -> None:
    window, _, service = desktop
    _go_to(window, "Virtual Machines")
    start = _widget(window, QPushButton, "startButton")
    stop = _widget(window, QPushButton, "stopButton")
    open_vm = _widget(window, QPushButton, "openButton")
    vm_list = _widget(window, QListWidget, "vmList")
    qtbot.waitUntil(lambda: vm_list.count() == 1)
    vm_list.setCurrentRow(0)
    qtbot.waitUntil(start.isEnabled)
    start.click()
    try:
        qtbot.waitUntil(service.entered_start.is_set)
        _go_to(window, "Dashboard")
        assert _widget(window, QLabel, "dashboardStatus").isVisible()
        assert not start.isEnabled()
    finally:
        service.finish_start.set()
    _go_to(window, "Virtual Machines")
    qtbot.waitUntil(stop.isEnabled)
    assert open_vm.isEnabled()
    stop.click()
    qtbot.waitUntil(start.isEnabled)
    assert not open_vm.isEnabled()


def test_passive_vm_poll_leaves_actions_available_and_discards_stale_results(
    qtbot: QtBot, desktop: Desktop, monkeypatch: pytest.MonkeyPatch
) -> None:
    window, _, service = desktop
    _go_to(window, "Virtual Machines")
    start = _widget(window, QPushButton, "startButton")
    stop = _widget(window, QPushButton, "stopButton")
    status = _widget(window, QLabel, "vmStatus")
    qtbot.waitUntil(start.isEnabled)
    window.poll.stop()
    original = service.status
    stale = replace(original(VM_ID), name="Stale VM poll")
    poll_started, release_poll, committed = Event(), Event(), Event()
    seen: list[str] = []

    def delayed_status(target: UUID | None) -> VMStatus:
        if not poll_started.is_set():
            poll_started.set()
            if not release_poll.wait(3):
                raise VirtualizationError("Test did not release pending status")
            return stale
        return original(target)

    show_status = window._show_status

    def record_status(result: VMStatus) -> None:
        seen.append(result.name)
        show_status(result)
        if result.active:
            committed.set()

    monkeypatch.setattr(service, "status", delayed_status)
    monkeypatch.setattr(window, "_show_status", record_status)
    window.poll.timeout.emit()
    try:
        qtbot.waitUntil(poll_started.is_set)
        assert start.isEnabled()
        service.finish_start.set()
        start.click()
        qtbot.waitUntil(committed.is_set)
        assert stop.isEnabled()
        assert "Running" in status.text()
    finally:
        release_poll.set()
    qtbot.waitUntil(lambda: not window.status_tasks.busy)
    assert "Stale VM poll" not in seen


def test_manager_surfaces_vm_errors_and_allows_retry(
    qtbot: QtBot, desktop: Desktop
) -> None:
    window, _, service = desktop
    service.start_error = "libvirt denied permission to start test VM"
    service.finish_start.set()
    _go_to(window, "Virtual Machines")
    vm_list = _widget(window, QListWidget, "vmList")
    qtbot.waitUntil(lambda: vm_list.count() == 1)
    vm_list.setCurrentRow(0)
    start = _widget(window, QPushButton, "startButton")
    task_status = _widget(window, QLabel, "taskStatus")
    qtbot.waitUntil(start.isEnabled)
    start.click()
    qtbot.waitUntil(lambda: "libvirt denied permission" in task_status.text())
    qtbot.waitUntil(start.isEnabled)


def test_manager_submits_validated_creation_dialog(
    qtbot: QtBot, desktop: Desktop, tmp_path: Path
) -> None:
    window, _, service = desktop
    _go_to(window, "Virtual Machines")
    create = _widget(window, QPushButton, "createButton")
    qtbot.waitUntil(create.isEnabled)
    source = ImageInput(
        tmp_path / "base.qcow2",
        tmp_path / "image.json",
        tmp_path / "image.sig",
        tmp_path / "trusted.gpg",
    )

    def fill_dialog() -> None:
        dialog = window.findChild(CreateVMDialog)
        assert dialog is not None
        buttons = dialog.findChild(QDialogButtonBox)
        assert buttons is not None
        submit = buttons.button(QDialogButtonBox.StandardButton.Ok)
        submit.click()
        assert _widget(dialog, QLabel, "createError").text() == "Enter a VM name."
        for field, value in {
            "vmName": "new-vm",
            "guestImage": str(source.image),
            "manifest": str(source.manifest),
            "signature": str(source.signature),
            "keyring": str(source.keyring),
            "guestPassword": service.guest_secret,
            "confirmPassword": service.guest_secret,
        }.items():
            _widget(dialog, QLineEdit, field).setText(value)
        submit.click()
        assert not _widget(dialog, QLineEdit, "guestPassword").text()

    QTimer.singleShot(0, fill_dialog)
    create.click()
    qtbot.waitUntil(lambda: service.created is not None and not window.tasks.busy)
    assert service.created == ("new-vm", source, VMDefaults())


def _session() -> FocusSession:
    started = datetime.now(UTC) - timedelta(minutes=3, seconds=30)
    return FocusSession(
        vm=VM_ID,
        vm_name=VM_NAME,
        profile=PROFILE_ID,
        network="strict",
        state="active",
        started_at=started.isoformat(),
        detail=None,
    )


class _SessionService:
    def __init__(self) -> None:
        self.current: FocusSession | None = None
        self.started: tuple[UUID | None, str, str] | None = None
        self.started_signal = Event()
        self.finish_start = Event()

    def status(self) -> FocusSession | None:
        return self.current

    def start(
        self,
        target: UUID | None,
        profile: str,
        network: str,
        *,
        duration_minutes: int | None = None,
        cage: bool = False,
    ) -> FocusSession:
        self.started_signal.set()
        if not self.finish_start.wait(timeout=3):
            raise SessionError("Test did not release pending focus session start")
        self.started = (target, profile, network)
        session = _session()
        self.current = session
        return session

    def reopen(self) -> FocusSession:
        assert self.current is not None
        session = replace(self.current, state="active", detail=None)
        self.current = session
        return session

    def end(self) -> None:
        self.current = None


def test_end_session_keeps_stale_passive_result_from_reopening_controls(
    qtbot: QtBot, desktop: Desktop, monkeypatch: pytest.MonkeyPatch
) -> None:
    window, _, _ = desktop
    sessions = _SessionService()
    sessions.current = _session()
    monkeypatch.setattr(window, "session_service", sessions)
    _go_to(window, "Focus Sessions")
    end = _widget(window, QPushButton, "sessionEndButton")
    status = _widget(window, QLabel, "sessionStatus")
    qtbot.waitUntil(end.isEnabled)
    window.poll.stop()
    poll_started, release_poll = Event(), Event()
    stale = replace(sessions.current, vm_name="Stale session poll")
    seen: list[str] = []

    def delayed_status() -> FocusSession | None:
        if not poll_started.is_set():
            poll_started.set()
            if not release_poll.wait(3):
                raise SessionError("Test did not release pending status")
            return stale
        return sessions.current

    show_session = window._show_session

    def record_session(result: FocusSession | None) -> None:
        if result is not None:
            seen.append(result.vm_name)
        show_session(result)

    monkeypatch.setattr(sessions, "status", delayed_status)
    monkeypatch.setattr(window, "_show_session", record_session)
    window.poll.timeout.emit()
    try:
        qtbot.waitUntil(poll_started.is_set)
        assert end.isEnabled()
        end.click()
        qtbot.waitUntil(lambda: "No focus session" in status.text())
    finally:
        release_poll.set()
    qtbot.waitUntil(lambda: not window.status_tasks.busy)
    assert "Stale session poll" not in seen
    assert not end.isEnabled()


def test_manager_starts_offline_session_and_refreshes_viewer_state(
    qtbot: QtBot, desktop: Desktop, monkeypatch: pytest.MonkeyPatch
) -> None:
    window, _, _ = desktop
    sessions = _SessionService()
    monkeypatch.setattr(window, "session_service", sessions)
    _go_to(window, "Focus Sessions")
    target = _widget(window, QLabel, "sessionVM")
    network = _widget(window, QComboBox, "sessionNetwork")
    start = _widget(window, QPushButton, "sessionStartButton")
    reopen = _widget(window, QPushButton, "sessionReopenButton")
    end = _widget(window, QPushButton, "sessionEndButton")
    refresh = _widget(window, QPushButton, "sessionRefreshButton")
    status = _widget(window, QLabel, "sessionStatus")
    qtbot.waitUntil(start.isEnabled)
    assert target.text() == VM_NAME
    assert network.currentText() == "Balanced"
    assert network.count() == 2
    network.setCurrentIndex(network.findText("Strict"))
    start.click()
    try:
        qtbot.waitUntil(sessions.started_signal.is_set)
        assert not start.isEnabled()
        _go_to(window, "Dashboard")
        assert _widget(window, QLabel, "dashboardStatus").isVisible()
    finally:
        sessions.finish_start.set()
    _go_to(window, "Focus Sessions")
    qtbot.waitUntil(end.isEnabled)
    assert sessions.started == (VM_ID, PROFILE_ID, "strict")
    assert "Strict (offline)" in status.text()
    assert "elapsed" in status.text()
    assert not reopen.isEnabled()

    sessions.current = replace(
        _session(), state="disconnected", detail="Viewer exited."
    )
    refresh.click()
    qtbot.waitUntil(reopen.isEnabled)
    assert "Viewer exited." in status.text()
    reopen.click()
    qtbot.waitUntil(lambda: "Active" in status.text())
    assert not reopen.isEnabled()
    end.click()
    qtbot.waitUntil(start.isEnabled)
    assert sessions.current is None
    assert "No focus session" in status.text()


def test_manager_offers_end_when_session_recovery_is_unresolved(
    qtbot: QtBot, desktop: Desktop, monkeypatch: pytest.MonkeyPatch
) -> None:
    window, _, _ = desktop
    sessions = _SessionService()
    sessions.current = replace(
        _session(), state="unresolved", detail="The VM definition changed."
    )
    monkeypatch.setattr(window, "session_service", sessions)
    qtbot.waitUntil(lambda: not window.tasks.busy and window.status is not None)
    _go_to(window, "Focus Sessions")

    end = _widget(window, QPushButton, "sessionEndButton")
    qtbot.waitUntil(end.isEnabled)
    assert not _widget(window, QPushButton, "sessionStartButton").isEnabled()
    assert not _widget(window, QPushButton, "sessionReopenButton").isEnabled()
    assert (
        "The VM definition changed." in _widget(window, QLabel, "sessionStatus").text()
    )


def test_contest_clock_refreshes_without_waiting_for_session_poll(
    qtbot: QtBot, desktop: Desktop, monkeypatch: pytest.MonkeyPatch
) -> None:
    window, _, _ = desktop
    sessions = _SessionService()
    sessions.current = replace(
        _session(),
        profile="contest",
        deadline=(datetime.now(UTC) + timedelta(seconds=90)).isoformat(),
    )
    monkeypatch.setattr(window, "session_service", sessions)
    _go_to(window, "Focus Sessions")
    end = _widget(window, QPushButton, "sessionEndButton")
    label = _widget(window, QLabel, "sessionStatus")
    qtbot.waitUntil(end.isEnabled)
    assert "remaining" in label.text()
    window.poll.stop()
    window.session_clock.stop()

    class ExpiredClock(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> "ExpiredClock":
            return cls.fromtimestamp(datetime.now(UTC).timestamp() + 120, tz)

    monkeypatch.setattr("focusvm.sessions.datetime", ExpiredClock)
    window.session_clock.timeout.emit()
    assert "Contest timer expired." in label.text()
    assert not window.status_tasks.busy
    assert end.isEnabled()
