"""First-run storage selection and host diagnostics."""

from collections.abc import Sequence

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
    QWizard,
    QWizardPage,
)

from ..config import ConfigError, ConfigPaths, Settings
from ..diagnostics import Diagnostic, diagnose
from ..setup import preferred_pool, setup_pools
from .tasks import TaskRunner

_CHECK_LABELS = {
    "architecture": "Host architecture",
    "kvm_device": "KVM device",
    "config_directory": "Settings directory",
    "state_directory": "State directory",
    "cache_directory": "Cache directory",
    "data_directory": "Data directory",
    "libvirt": "Libvirt connection",
    "vm_capabilities": "Virtual machine capabilities",
    "storage_pool": "Storage pool",
    "desktop": "Desktop session",
    "focus_viewer": "Focus viewer",
    "guest_image": "Guest image",
    "qemu-img": "qemu-img tool",
    "gpgv": "Signature verifier (gpgv)",
    "openssl": "OpenSSL tool",
    "xorriso": "ISO builder (xorriso)",
    "virt-viewer": "virt-viewer",
}
_STATUS_LABELS = {"ok": "Passed", "warning": "Warning", "error": "Failed"}


class SetupWizard(QWizard):
    """Select a libvirt pool and confirm that the host can use it."""

    def __init__(self, paths: ConfigPaths, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("FocusVM setup")
        self.setMinimumSize(580, 430)
        self.setButtonText(QWizard.WizardButton.FinishButton, "Save settings")
        self._host_page = _HostPage(paths, self)
        self.addPage(self._host_page)

    @property
    def storage_pool(self) -> str:
        """Return the validated selection after the wizard is accepted."""
        return self._host_page.checked_pool or ""

    def done(self, result: int) -> None:
        if self._host_page.checking:
            return
        if result == QDialog.DialogCode.Accepted and not self._host_page.isComplete():
            return
        super().done(result)


class _HostPage(QWizardPage):
    def __init__(self, paths: ConfigPaths, parent: QWizard) -> None:
        super().__init__(parent)
        self._paths = paths
        self._tasks = TaskRunner(self)
        self.checked_pool: str | None = None
        self.checking = False

        self.setTitle("Choose a storage pool")
        self.setSubTitle(
            "Enter the name of an active directory-backed libvirt pool for guest disks."
        )

        self._pool = QLineEdit()
        self._pool.setObjectName("setupStoragePool")
        self._pool.setPlaceholderText("focusvm")
        self._pool.textChanged.connect(self._clear_checks)
        self._pool.returnPressed.connect(self._run_checks)

        self._check = QPushButton("&Check host")
        self._check.setObjectName("setupCheckHost")
        self._check.setAutoDefault(False)
        self._check.clicked.connect(self._run_checks)

        pool_row = QHBoxLayout()
        pool_row.addWidget(self._pool, 1)
        pool_row.addWidget(self._check)
        pool_form = QFormLayout()
        pool_label = QLabel("Storage &pool")
        pool_label.setBuddy(self._pool)
        pool_form.addRow(pool_label, pool_row)

        hint = QLabel("Leave the name blank to detect a pool, or enter one to use it.")
        hint.setWordWrap(True)
        hint.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        self._summary = QLabel("Check the host before saving settings.")
        self._summary.setObjectName("setupCheckSummary")
        self._summary.setTextFormat(Qt.TextFormat.PlainText)
        self._summary.setWordWrap(True)

        self._results = QTableWidget(0, 3)
        self._results.setObjectName("setupCheckResults")
        self._results.setAccessibleName("Host check results")
        self._results.setHorizontalHeaderLabels(["Check", "Result", "Details"])
        self._results.verticalHeader().hide()
        self._results.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._results.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self._results.setWordWrap(True)
        header = self._results.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self._results.verticalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )

        layout = QVBoxLayout(self)
        layout.addLayout(pool_form)
        layout.addWidget(hint)
        layout.addWidget(self._summary)
        layout.addWidget(self._results, 1)

    def isComplete(self) -> bool:
        return not self.checking and self.checked_pool is not None

    def _clear_checks(self) -> None:
        self.checked_pool = None
        self._results.setRowCount(0)
        self._summary.setText("Check the host before saving settings.")
        self._check.setEnabled(not self.checking)
        self.completeChanged.emit()

    def _run_checks(self) -> None:
        if self.checking or not self._check.isEnabled():
            return
        self._clear_checks()
        selected = self._pool.text()
        try:
            if selected:
                selected = Settings.for_pool(selected).application.storage_pool
        except ConfigError as error:
            self._summary.setText(f"Invalid pool name. {error}")
            return

        self._set_checking(True)
        self._summary.setText("Checking the host and storage pools…")
        paths = self._paths

        def inspect() -> tuple[str, list[Diagnostic]]:
            if selected:
                return selected, diagnose(paths, selected)
            pools = setup_pools()
            automatic = preferred_pool(pools)
            if automatic is not None:
                return automatic, diagnose(paths, automatic)
            detail = (
                f"Choose a storage pool from {', '.join(pools)}."
                if pools
                else "No active directory-backed storage pool has free space."
            )
            return "", [
                Diagnostic("storage_pool", "error", detail),
                *(
                    check
                    for check in diagnose(paths, None)
                    if check.name != "storage_pool"
                ),
            ]

        def loaded(result: tuple[str, list[Diagnostic]]) -> None:
            pool, checks = result
            if pool and not selected:
                self._pool.setText(pool)
            self._show_checks(pool, checks)

        if not self._tasks.submit(
            "Check host",
            inspect,
            loaded,
            self._show_error,
        ):
            self._show_error("Try again when the current check has finished.")

    def _set_checking(self, checking: bool) -> None:
        self.checking = checking
        self._pool.setEnabled(not checking)
        self._check.setEnabled(not checking)
        self.wizard().button(QWizard.WizardButton.CancelButton).setEnabled(not checking)
        self.completeChanged.emit()

    def _show_checks(self, pool: str, checks: Sequence[Diagnostic]) -> None:
        self._results.setRowCount(len(checks))
        for row, check in enumerate(checks):
            fields = (
                _CHECK_LABELS.get(check.name, check.name),
                _STATUS_LABELS[check.status],
                check.detail,
            )
            for column, field in enumerate(fields):
                self._results.setItem(row, column, QTableWidgetItem(field))

        pool_valid = any(
            check.name == "storage_pool" and check.status == "ok" for check in checks
        )
        failed = any(check.status == "error" for check in checks)
        if not failed and pool_valid and self._pool.text() == pool:
            self.checked_pool = pool
            self._summary.setText(
                "Host checks passed. Review any warnings and save your settings."
            )
        else:
            self._summary.setText(
                "Resolve the failed checks or choose another pool, then check again."
            )
        self._set_checking(False)

    def _show_error(self, message: str) -> None:
        self._summary.setText(f"Host checks could not finish. {message}")
        self._set_checking(False)
