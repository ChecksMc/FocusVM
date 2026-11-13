"""Native dialogs for entering VM settings and guest image inputs."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import override

from PySide6.QtCore import QRegularExpression, Qt
from PySide6.QtGui import QRegularExpressionValidator
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..config import MAX_TOML_INTEGER, ConfigError, VMDefaults
from ..provisioning import GUEST_USERNAME, ImageInput


@dataclass(frozen=True, slots=True)
class CreateRequest:
    name: str
    source: ImageInput
    password: str = field(repr=False)
    resources: VMDefaults


class CreateVMDialog(QDialog):
    def __init__(self, defaults: VMDefaults, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Create virtual machine")
        self.setModal(True)
        self.request: CreateRequest | None = None

        self._name = QLineEdit()
        self._name.setObjectName("vmName")
        self._cpus = positive_input("vmCpus", defaults.cpus)
        self._memory = positive_input("vmMemory", defaults.memory_mib)
        self._disk = positive_input("vmDisk", defaults.disk_gib)
        self._password = _password_input("guestPassword")
        self._confirm = _password_input("confirmPassword")
        self._error = _error_label("createError")

        form = QFormLayout()
        form.addRow("VM &name", self._name)
        self._image = self._add_file_input(form, "&Guest image", "guestImage")
        self._manifest = self._add_file_input(form, "&Manifest", "manifest")
        self._signature = self._add_file_input(form, "Manifest &signature", "signature")
        self._keyring = self._add_file_input(form, "Trusted &keyring", "keyring")
        self._keyring.setAccessibleDescription(
            "Choose a keyring you trust to verify the guest image manifest."
        )
        form.addRow("&Virtual CPUs", self._cpus)
        form.addRow("&RAM (MiB)", self._memory)
        form.addRow("&Disk (GiB)", self._disk)
        form.addRow(f"Guest &password ({GUEST_USERNAME})", self._password)
        form.addRow("Con&firm password", self._confirm)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self._error)
        layout.addWidget(_buttons(self, "&Create"))
        self._name.setFocus()

    def _add_file_input(
        self, form: QFormLayout, label_text: str, name: str
    ) -> QLineEdit:
        editor = QLineEdit()
        editor.setObjectName(name)
        label = QLabel(label_text)
        label.setBuddy(editor)
        browse = QPushButton("Browse…")
        browse.setAutoDefault(False)
        title = label_text.replace("&", "")
        browse.setAccessibleName(f"Browse for {title.lower()}")

        def choose_file() -> None:
            filename, _ = QFileDialog.getOpenFileName(self, title, editor.text())
            if filename:
                editor.setText(filename)

        browse.clicked.connect(choose_file)
        row = QHBoxLayout()
        row.addWidget(editor)
        row.addWidget(browse)
        form.addRow(label, row)
        return editor

    @override
    def accept(self) -> None:
        self._error.clear()
        required = (
            (self._name, "Enter a VM name."),
            (self._image, "Select a guest image."),
            (self._manifest, "Select an image manifest."),
            (self._signature, "Select a manifest signature."),
            (self._keyring, "Select a trusted keyring."),
        )
        for editor, message in required:
            if not editor.text().strip():
                self._error.setText(message)
                editor.setFocus()
                return

        password = self._password.text()
        if not password:
            self._error.setText("Enter a password for the guest account.")
            self._password.setFocus()
            return
        if password != self._confirm.text():
            self._error.setText("Guest passwords do not match.")
            self._confirm.setFocus()
            return

        try:
            resources = VMDefaults(
                cpus=positive_value(self._cpus, "Virtual CPUs"),
                memory_mib=positive_value(self._memory, "RAM"),
                disk_gib=positive_value(self._disk, "Disk capacity"),
            )
        except ConfigError as error:
            self._error.setText(str(error))
            return

        self.request = CreateRequest(
            name=self._name.text(),
            source=ImageInput(
                image=Path(self._image.text()),
                manifest=Path(self._manifest.text()),
                signature=Path(self._signature.text()),
                keyring=Path(self._keyring.text()),
            ),
            password=password,
            resources=resources,
        )
        self._clear_passwords()
        super().accept()

    @override
    def reject(self) -> None:
        self.request = None
        self._clear_passwords()
        super().reject()

    def _clear_passwords(self) -> None:
        self._password.clear()
        self._confirm.clear()


class ResourceDialog(QDialog):
    def __init__(
        self, cpus: int, memory_mib: int, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Configure VM resources")
        self.setModal(True)
        self.resources: tuple[int, int] | None = None

        self._cpus = positive_input("vmCpus", cpus)
        self._memory = positive_input("vmMemory", memory_mib)
        self._error = _error_label("resourceError")

        form = QFormLayout()
        form.addRow("&Virtual CPUs", self._cpus)
        form.addRow("&RAM (MiB)", self._memory)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self._error)
        layout.addWidget(_buttons(self, "&Save"))

    @override
    def accept(self) -> None:
        self._error.clear()
        try:
            resources = VMDefaults(
                cpus=positive_value(self._cpus, "Virtual CPUs"),
                memory_mib=positive_value(self._memory, "RAM"),
            )
        except ConfigError as error:
            self._error.setText(str(error))
            return
        self.resources = (resources.cpus, resources.memory_mib)
        super().accept()

    @override
    def reject(self) -> None:
        self.resources = None
        super().reject()


def positive_input(name: str, value: int) -> QLineEdit:
    editor = QLineEdit(str(value))
    editor.setObjectName(name)
    editor.setInputMethodHints(Qt.InputMethodHint.ImhDigitsOnly)
    editor.setValidator(
        QRegularExpressionValidator(QRegularExpression("[0-9]{1,19}"), editor)
    )
    return editor


def positive_value(editor: QLineEdit, label: str) -> int:
    if editor.hasAcceptableInput():
        value = int(editor.text())
        if 1 <= value <= MAX_TOML_INTEGER:
            return value
    editor.setFocus()
    raise ConfigError(f"Enter a positive whole number for {label}.")


def _password_input(name: str) -> QLineEdit:
    editor = QLineEdit()
    editor.setObjectName(name)
    editor.setEchoMode(QLineEdit.EchoMode.Password)
    return editor


def _error_label(name: str) -> QLabel:
    label = QLabel()
    label.setObjectName(name)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return label


def _buttons(dialog: QDialog, accept_text: str) -> QDialogButtonBox:
    buttons = QDialogButtonBox(
        QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
    )
    accept_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
    accept_button.setText(accept_text)
    accept_button.setDefault(True)
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    return buttons
