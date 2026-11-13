from pathlib import Path
from threading import Event

import pytest
from PySide6.QtWidgets import QLabel, QLineEdit, QPushButton, QTableWidget, QWizard
from pytestqt.qtbot import QtBot

from focusvm.config import ConfigPaths
from focusvm.diagnostics import Diagnostic
from focusvm.gui import setup_wizard
from focusvm.gui.setup_wizard import SetupWizard
from focusvm.virtualization import VirtualizationError


def _wizard(qtbot: QtBot, tmp_path: Path) -> SetupWizard:
    paths = ConfigPaths.from_environment(environment={}, home=tmp_path)
    wizard = SetupWizard(paths)
    qtbot.addWidget(wizard)
    wizard.show()
    return wizard


def _pool(wizard: SetupWizard) -> QLineEdit:
    widget = wizard.findChild(QLineEdit, "setupStoragePool")
    assert widget is not None
    return widget


def _check(wizard: SetupWizard) -> QPushButton:
    widget = wizard.findChild(QPushButton, "setupCheckHost")
    assert widget is not None
    return widget


def _summary(wizard: SetupWizard) -> QLabel:
    widget = wizard.findChild(QLabel, "setupCheckSummary")
    assert widget is not None
    return widget


def _results(wizard: SetupWizard) -> QTableWidget:
    widget = wizard.findChild(QTableWidget, "setupCheckResults")
    assert widget is not None
    return widget


def test_setup_checks_pool_then_accepts_warnings(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[ConfigPaths, str | None]] = []

    def host_checks(paths: ConfigPaths, pool: str | None) -> list[Diagnostic]:
        calls.append((paths, pool))
        return [
            Diagnostic("storage_pool", "ok", "1,024 bytes available."),
            Diagnostic("guest_image", "warning", "Provide a signed image."),
        ]

    monkeypatch.setattr(setup_wizard, "diagnose", host_checks)
    wizard = _wizard(qtbot, tmp_path)
    finish = wizard.button(QWizard.WizardButton.FinishButton)
    assert not finish.isEnabled()

    _pool(wizard).setText("../invalid")
    _check(wizard).click()
    assert "Invalid pool name" in _summary(wizard).text()
    assert calls == []

    _pool(wizard).setText("workspace")
    _check(wizard).click()
    qtbot.waitUntil(finish.isEnabled)
    assert _results(wizard).rowCount() == 2
    item = _results(wizard).item(1, 1)
    assert item is not None
    assert item.text() == "Warning"

    with qtbot.waitSignal(wizard.accepted):
        finish.click()
    assert wizard.storage_pool == "workspace"
    assert calls == [
        (ConfigPaths.from_environment(environment={}, home=tmp_path), "workspace")
    ]
    assert list(tmp_path.iterdir()) == []


def test_setup_requires_pool_and_all_required_checks_to_pass(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def host_checks(_paths: ConfigPaths, pool: str | None) -> list[Diagnostic]:
        if pool == "missing":
            return [Diagnostic("storage_pool", "warning", "Pool could not be checked.")]
        return [
            Diagnostic("storage_pool", "ok", "Pool is active."),
            Diagnostic("vm_capabilities", "error", "QEMU requires SPICE support."),
        ]

    monkeypatch.setattr(setup_wizard, "diagnose", host_checks)
    wizard = _wizard(qtbot, tmp_path)
    finish = wizard.button(QWizard.WizardButton.FinishButton)

    for pool in ("missing", "workspace"):
        _pool(wizard).setText(pool)
        _check(wizard).click()
        qtbot.waitUntil(_check(wizard).isEnabled)
        assert not finish.isEnabled()
        assert wizard.storage_pool == ""
        wizard.accept()
        assert wizard.isVisible()

    item = _results(wizard).item(1, 2)
    assert item is not None
    assert item.text() == "QEMU requires SPICE support."


def test_setup_keeps_worker_alive_and_invalidates_edited_pool(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    running = Event()
    release = Event()

    def host_checks(_paths: ConfigPaths, _pool: str | None) -> list[Diagnostic]:
        running.set()
        if not release.wait(5):
            raise VirtualizationError("The test did not release the worker")
        return [Diagnostic("storage_pool", "ok", "Pool is active.")]

    monkeypatch.setattr(setup_wizard, "diagnose", host_checks)
    wizard = _wizard(qtbot, tmp_path)
    finish = wizard.button(QWizard.WizardButton.FinishButton)
    cancel = wizard.button(QWizard.WizardButton.CancelButton)

    _pool(wizard).setText("workspace")
    _check(wizard).click()
    try:
        qtbot.waitUntil(running.is_set)
        assert not finish.isEnabled()
        assert not cancel.isEnabled()
        wizard.reject()
        wizard.close()
        assert wizard.isVisible()
    finally:
        release.set()
        qtbot.waitUntil(_check(wizard).isEnabled)

    assert finish.isEnabled()
    assert cancel.isEnabled()
    _pool(wizard).setText("other")
    assert not finish.isEnabled()
    assert wizard.storage_pool == ""
    assert _results(wizard).rowCount() == 0


def test_setup_surfaces_host_query_exception(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def host_checks(_paths: ConfigPaths, _pool: str | None) -> list[Diagnostic]:
        raise VirtualizationError("Cannot connect to qemu:///system.")

    monkeypatch.setattr(setup_wizard, "diagnose", host_checks)
    wizard = _wizard(qtbot, tmp_path)
    _pool(wizard).setText("workspace")
    _check(wizard).click()
    qtbot.waitUntil(_check(wizard).isEnabled)

    assert "Cannot connect to qemu:///system." in _summary(wizard).text()
    assert not wizard.button(QWizard.WizardButton.FinishButton).isEnabled()
