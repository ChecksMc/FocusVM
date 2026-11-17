"""Host controls for the local reference library."""

from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QProcess, Qt
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..resources import Resource, ResourceLibrary, ViewerCommand
from .tasks import TaskRunner


class ResourcesPage(QWidget):
    def __init__(self, data_dir: Path) -> None:
        super().__init__()
        self.library = ResourceLibrary(data_dir)
        self.tasks = TaskRunner(self)
        self._resources: tuple[Resource, ...] = ()
        self._visible: tuple[Resource, ...] = ()

        self.search = QLineEdit()
        self.search.setObjectName("resourceSearch")
        self.search.setAccessibleName("Search resource filenames")
        self.search.setPlaceholderText("Search filenames")
        self.search.textChanged.connect(self._render)

        self.entries = QListWidget()
        self.entries.setObjectName("resourceEntries")
        self.entries.setAccessibleName("Local resources")
        self.entries.currentRowChanged.connect(self._sync_actions)
        self.entries.itemDoubleClicked.connect(self._open)

        self.details = QLabel()
        self.details.setTextFormat(Qt.TextFormat.PlainText)
        self.details.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.details.setWordWrap(True)
        self.status = QLabel("Open this page to load your library.")
        self.status.setObjectName("resourceStatus")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.status.setWordWrap(True)

        self.add_button = QPushButton("Import File…")
        self.add_button.setObjectName("resourceAdd")
        self.add_button.clicked.connect(self._add)
        self.open_button = QPushButton("Open")
        self.open_button.setObjectName("resourceOpen")
        self.open_button.clicked.connect(self._open)
        self.remove_button = QPushButton("Remove…")
        self.remove_button.setObjectName("resourceRemove")
        self.remove_button.clicked.connect(self._remove)
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(self.refresh)

        actions = QHBoxLayout()
        actions.addWidget(self.add_button)
        actions.addWidget(self.open_button)
        actions.addWidget(self.remove_button)
        actions.addStretch()
        actions.addWidget(self.refresh_button)
        layout = QVBoxLayout(self)
        layout.addWidget(self.search)
        layout.addWidget(self.entries, 1)
        layout.addWidget(self.details)
        layout.addLayout(actions)
        layout.addWidget(self.status)
        self._sync_actions()

    def refresh(self) -> None:
        self._submit("Loading resources…", self.library.list, self._show_resources)

    def _show_resources(
        self, items: tuple[Resource, ...], *, selected_id: str | None = None
    ) -> None:
        self._resources = items
        self.status.setText(f"{len(items)} files in the library.")
        self._render(selected_id=selected_id)

    def _render(self, *, selected_id: str | None = None) -> None:
        previous = self._selected()
        if selected_id is None and previous is not None:
            selected_id = previous.id
        term = self.search.text().strip().casefold()
        self.entries.clear()
        self._visible = tuple(
            item for item in self._resources if term in item.name.casefold()
        )
        metrics = QFontMetrics(self.entries.font())
        for row, resource in enumerate(self._visible):
            title = metrics.elidedText(resource.name, Qt.TextElideMode.ElideMiddle, 360)
            entry = QListWidgetItem(f"{title}  [{resource.kind}]")
            entry.setData(Qt.ItemDataRole.AccessibleTextRole, resource.name)
            entry.setToolTip(resource.name)
            self.entries.addItem(entry)
            if selected_id == resource.id:
                self.entries.setCurrentRow(row)
        self._sync_actions()

    def _selected(self) -> Resource | None:
        row = self.entries.currentRow()
        return self._visible[row] if 0 <= row < len(self._visible) else None

    def _sync_actions(self) -> None:
        resource = self._selected()
        ready = not self.tasks.busy
        self.add_button.setEnabled(ready)
        self.refresh_button.setEnabled(ready)
        self.remove_button.setEnabled(ready and resource is not None)
        self.open_button.setEnabled(
            ready
            and resource is not None
            and resource.kind not in ("git-bundle", "wheel")
        )
        self.details.setText(
            f"{resource.size_bytes:,} bytes · ID {resource.id}"
            if resource is not None
            else ""
        )

    def _add(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(
            self,
            "Import a local resource",
            "",
            "Supported files (*.pdf *.PDF *.htm *.html *.md *.markdown *.zim *.bundle *.whl);;All files (*)",
        )
        if not filename:
            return

        def load() -> tuple[Resource, tuple[Resource, ...]]:
            item = self.library.add(Path(filename))
            return item, self.library.list()

        def show(result: tuple[Resource, tuple[Resource, ...]]) -> None:
            item, items = result
            self._show_resources(items, selected_id=item.id)
            self.status.setText(f"Imported {item.name}.")

        self._submit("Importing resource…", load, show)

    def _open(self) -> None:
        resource = self._selected()
        if resource is None or not self.open_button.isEnabled():
            return

        def launch(command: ViewerCommand) -> None:
            program, arguments = command
            started, _pid = QProcess.startDetached(program, arguments)
            if started:
                self.status.setText(f"Opened {resource.name}.")
            else:
                self.status.setText(f"Could not start the viewer for {resource.name}.")

        self._submit(
            "Preparing resource…",
            lambda: self.library.open_command(resource.id),
            launch,
        )

    def _remove(self) -> None:
        resource = self._selected()
        if resource is None:
            return
        dialog = QMessageBox(self)
        dialog.setWindowTitle("Remove resource")
        dialog.setTextFormat(Qt.TextFormat.PlainText)
        dialog.setText(f"Remove {resource.name} from the library?")
        dialog.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel
        )
        dialog.setDefaultButton(QMessageBox.StandardButton.Cancel)
        dialog.setEscapeButton(QMessageBox.StandardButton.Cancel)
        dialog.exec()
        # exec() returns a plain int, which does not reliably compare equal to the
        # Python enum used by PySide6. Ask which standard button was clicked instead.
        clicked = dialog.clickedButton()
        confirmed = (
            clicked is not None
            and dialog.standardButton(clicked) == QMessageBox.StandardButton.Yes
        )
        dialog.deleteLater()
        if not confirmed:
            return

        def remove() -> tuple[Resource, ...]:
            self.library.remove(resource.id)
            return self.library.list()

        def show(items: tuple[Resource, ...]) -> None:
            self._show_resources(items)
            self.status.setText(f"Removed {resource.name} from the library.")

        self._submit("Removing resource…", remove, show)

    def _submit[T](
        self,
        status: str,
        operation: Callable[[], T],
        on_success: Callable[[T], None],
    ) -> None:
        def success(result: T) -> None:
            self._sync_actions()
            on_success(result)

        def failed(message: str) -> None:
            self.status.setText(message)
            self._sync_actions()

        if self.tasks.submit(status, operation, success, failed):
            self.status.setText(status)
            self._sync_actions()
