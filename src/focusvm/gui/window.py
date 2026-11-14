"""Native manager for the shared FocusVM services."""

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

from PySide6.QtCore import QProcess, Qt, QTimer
from PySide6.QtGui import QCloseEvent, QGuiApplication
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..config import (
    THEMES,
    ConfigError,
    ConfigPaths,
    ConfigStore,
    Settings,
    Theme,
    VMDefaults,
)
from ..diagnostics import Diagnostic, diagnose
from ..profiles import PROFILES
from ..sessions import (
    MAX_CONTEST_MINUTES,
    OFFLINE_NETWORKS,
    FocusSession,
    SessionService,
)
from ..viewer import console_command
from ..virtualization import VMStatus
from ..vm import GIB, VMRecord, VMService
from .dialogs import CreateVMDialog, ResourceDialog, positive_input, positive_value
from .resources_page import ResourcesPage
from .setup_wizard import SetupWizard
from .tasks import TaskRunner

_SETTINGS_PAGE = 2
_SESSIONS_PAGE = 3
_RESOURCES_PAGE = 4


def _button(name: str, text: str) -> QPushButton:
    button = QPushButton(text)
    button.setObjectName(name)
    return button


def _label(name: str, text: str = "") -> QLabel:
    label = QLabel(text)
    label.setObjectName(name)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return label


class MainWindow(QMainWindow):
    def __init__(self, paths: ConfigPaths, *, show_wizard: bool = True) -> None:
        super().__init__()
        self.paths = paths
        self.store = ConfigStore(paths.config_file)
        self.service = VMService(paths.state_dir, paths.cache_dir)
        self.session_service = SessionService(paths.state_dir, self.service)
        self.tasks = TaskRunner(self)
        self.settings: Settings | None = None
        self.records: tuple[VMRecord, ...] = ()
        self.status: VMStatus | None = None
        self.session: FocusSession | None = None
        self.session_checked = False
        self.polling = False
        self.wizard: SetupWizard | None = None
        self.show_wizard = show_wizard
        self.setWindowTitle("FocusVM")
        self.resize(960, 640)
        self.setMinimumSize(680, 460)

        self.navigation = QListWidget()
        self.navigation.setObjectName("navigation")
        self.navigation.setAccessibleName("Pages")
        self.navigation.addItems(
            ["Dashboard", "Virtual Machines", "Settings", "Focus Sessions", "Resources"]
        )
        self.navigation.setFixedWidth(160)
        self.pages = QStackedWidget()
        self.pages.addWidget(self._dashboard())
        self.pages.addWidget(self._virtual_machines())
        self.pages.addWidget(self._settings_page())
        self.pages.addWidget(self._session_page())
        self.resources_page = ResourcesPage(paths.data_dir)
        self.pages.addWidget(self.resources_page)
        self.navigation.currentRowChanged.connect(self.pages.setCurrentIndex)
        self.navigation.currentRowChanged.connect(self._refresh_resources_page)
        self.pages.currentChanged.connect(self._page_changed)
        self.navigation.setCurrentRow(0)

        body = QHBoxLayout()
        body.addWidget(self.navigation)
        body.addWidget(self.pages, 1)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.addLayout(body, 1)
        self.task_status = _label("taskStatus", "Loading FocusVM…")
        self.task_status.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(self.task_status)
        self.setCentralWidget(content)

        self.poll = QTimer(self)
        self.poll.setInterval(5000)
        self.poll.timeout.connect(self._poll_selected)
        self.poll.start()
        self._sync_actions()
        QTimer.singleShot(0, self._load_settings)

    def _dashboard(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.dashboard_status = _label(
            "dashboardStatus", "Select a VM to see its status."
        )
        layout.addWidget(self.dashboard_status)
        lifecycle = QHBoxLayout()
        self.dashboard_start = _button("dashboardStartButton", "Start")
        self.dashboard_stop = _button("dashboardStopButton", "Shut Down")
        self.dashboard_open = _button("dashboardOpenButton", "Open Console")
        for button, action in (
            (self.dashboard_start, self._start),
            (self.dashboard_stop, self._stop),
            (self.dashboard_open, self._open_console),
        ):
            button.clicked.connect(action)
            lifecycle.addWidget(button)
        lifecycle.addStretch()
        layout.addLayout(lifecycle)
        checks = QHBoxLayout()
        self.host_check = _button("hostCheckButton", "Run Host Checks")
        self.host_check.clicked.connect(self._check_host)
        self.setup_button = _button("setupButton", "First-run Setup")
        self.setup_button.clicked.connect(self._open_setup)
        checks.addWidget(self.host_check)
        checks.addWidget(self.setup_button)
        manage = _button("manageButton", "Manage VMs")
        manage.clicked.connect(lambda: self.navigation.setCurrentRow(1))
        checks.addWidget(manage)
        checks.addStretch()
        layout.addLayout(checks)
        self.host_results = QPlainTextEdit()
        self.host_results.setObjectName("hostResults")
        self.host_results.setAccessibleName("Host check results")
        self.host_results.setReadOnly(True)
        self.host_results.setPlaceholderText(
            "Run host checks to inspect KVM, libvirt, and guest image tools."
        )
        layout.addWidget(self.host_results, 1)
        return page

    def _virtual_machines(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.vm_list = QListWidget()
        self.vm_list.setObjectName("vmList")
        self.vm_list.setAccessibleName("Managed virtual machines")
        self.vm_list.currentRowChanged.connect(self._selection_changed)
        layout.addWidget(self.vm_list, 1)
        self.vm_status = _label("vmStatus", "Select a VM to see its status.")
        self.vm_status.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(self.vm_status)

        actions = QHBoxLayout()
        self.create_button = _button("createButton", "Create VM…")
        self.refresh_button = _button("refreshButton", "Refresh")
        self.start_button = _button("startButton", "Start")
        self.stop_button = _button("stopButton", "Shut Down")
        self.open_button = _button("openButton", "Open Console")
        self.configure_button = _button("configureButton", "CPU and RAM…")
        self.create_button.clicked.connect(self._create)
        self.refresh_button.clicked.connect(self._refresh)
        self.start_button.clicked.connect(self._start)
        self.stop_button.clicked.connect(self._stop)
        self.open_button.clicked.connect(self._open_console)
        self.configure_button.clicked.connect(self._configure)
        for button in (
            self.create_button,
            self.refresh_button,
            self.start_button,
            self.stop_button,
            self.open_button,
            self.configure_button,
        ):
            actions.addWidget(button)
        actions.addStretch()
        layout.addLayout(actions)
        return page

    def _settings_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        form = QFormLayout()
        self.pool_input = QLineEdit()
        self.pool_input.setObjectName("storagePool")
        self.theme_input = QComboBox()
        self.theme_input.setObjectName("theme")
        self.theme_input.addItems([theme.capitalize() for theme in THEMES])
        defaults = VMDefaults()
        self.cpu_input = positive_input("defaultCPUs", defaults.cpus)
        self.memory_input = positive_input("defaultMemory", defaults.memory_mib)
        self.disk_input = positive_input("defaultDisk", defaults.disk_gib)
        self.default_vm = QComboBox()
        self.default_vm.setObjectName("defaultVM")
        self.default_choices: list[UUID | None] = [None]
        self.default_vm.addItem("Automatic")
        form.addRow("Storage &pool", self.pool_input)
        form.addRow("&Appearance", self.theme_input)
        form.addRow("Default &VM", self.default_vm)
        form.addRow("Default &vCPUs", self.cpu_input)
        form.addRow("Default &memory (MiB)", self.memory_input)
        form.addRow("Default &disk (GiB)", self.disk_input)
        layout.addLayout(form)
        self.settings_save = _button("settingsSaveButton", "Save Settings")
        self.settings_save.clicked.connect(self._save_settings)
        layout.addWidget(self.settings_save, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addStretch()
        return page

    def _session_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        form = QFormLayout()
        self.session_vm = _label(
            "sessionVM", "Select a VM on the Virtual Machines page."
        )
        vm_row = QHBoxLayout()
        vm_row.addWidget(self.session_vm, 1)
        self.session_choose_vm = _button("sessionChooseVMButton", "Choose VM…")
        self.session_choose_vm.clicked.connect(lambda: self.navigation.setCurrentRow(1))
        vm_row.addWidget(self.session_choose_vm)
        self.session_network = QComboBox()
        self.session_network.setObjectName("sessionNetwork")
        self.session_network.addItems([mode.capitalize() for mode in OFFLINE_NETWORKS])
        self.session_profile = QComboBox()
        self.session_profile.setObjectName("sessionProfile")
        self.session_profile.addItems([profile.name for profile in PROFILES.values()])
        self.session_profile.currentIndexChanged.connect(self._set_profile_options)
        self.session_duration = QSpinBox()
        self.session_duration.setObjectName("sessionDuration")
        self.session_duration.setRange(0, MAX_CONTEST_MINUTES)
        self.session_duration.setSpecialValueText("No timer")
        self.session_duration.setSuffix(" min")
        self.session_cage = QCheckBox("Use Cage on Wayland")
        self.session_cage.setObjectName("sessionCage")
        form.addRow("&Profile", self.session_profile)
        form.addRow("Virtual machine", vm_row)
        form.addRow("&Network", self.session_network)
        form.addRow("Contest timer", self.session_duration)
        form.addRow("Viewer", self.session_cage)
        layout.addLayout(form)
        layout.addWidget(
            _label(
                "sessionNetworkHelp", "Balanced and Strict both run the guest offline."
            )
        )
        self.session_status = _label("sessionStatus", "Reading focus session status…")
        self.session_status.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(self.session_status)
        actions = QHBoxLayout()
        self.session_start = _button("sessionStartButton", "Start Focus Session")
        self.session_reopen = _button("sessionReopenButton", "Reopen Viewer")
        self.session_end = _button("sessionEndButton", "End Focus Session")
        self.session_refresh = _button("sessionRefreshButton", "Refresh")
        for button, action in (
            (self.session_start, self._start_session),
            (self.session_reopen, self._reopen_session),
            (self.session_end, self._end_session),
            (self.session_refresh, self._poll_session),
        ):
            button.clicked.connect(action)
            actions.addWidget(button)
        actions.addStretch()
        layout.addLayout(actions)
        layout.addWidget(
            _label(
                "sessionExitHelp",
                "The regular viewer uses Shift+F12 to release input and Shift+F11 to leave "
                "fullscreen. For Cage, close its host window. End the session here or run "
                "focusvm session end from a host terminal. The guest stays running.",
            )
        )
        layout.addStretch()
        return page

    def _page_changed(self, index: int) -> None:
        if index == _SESSIONS_PAGE:
            self.session_checked = False
            self._poll_session_if_needed()
            self._sync_actions()

    def _poll_session_if_needed(self) -> None:
        if not self.session_checked and self.pages.currentIndex() == _SESSIONS_PAGE:
            self._poll_session()

    def _submit[T](
        self,
        label: str,
        operation: Callable[[], T],
        success: Callable[[T], None],
        *,
        failure: Callable[[str], None] | None = None,
    ) -> None:
        def finish(result: T) -> None:
            self.task_status.setText("")
            self._sync_actions()
            success(result)
            self._poll_session_if_needed()

        def failed(message: str) -> None:
            (failure or self._error)(message)
            self._poll_session_if_needed()

        if self.tasks.submit(label, operation, finish, failed):
            self.task_status.setText(label)
            self._sync_actions()

    def _error(self, message: str) -> None:
        self.task_status.setText(message)
        self._sync_actions()

    def _sync_actions(self) -> None:
        ready = not self.tasks.busy
        settings_ready = self.settings is not None and (ready or self.polling)
        active = self.status is not None and self.status.active
        self.host_check.setEnabled(ready)
        self.setup_button.setVisible(self.settings is None)
        self.setup_button.setEnabled(ready and self.wizard is None)
        self.vm_list.setEnabled(ready)
        self.refresh_button.setEnabled(ready)
        self.create_button.setEnabled(ready and self.settings is not None)
        self.settings_save.setEnabled(ready and self.settings is not None)
        for field in (
            self.pool_input,
            self.theme_input,
            self.default_vm,
            self.cpu_input,
            self.memory_input,
            self.disk_input,
        ):
            field.setEnabled(settings_ready)
        self.start_button.setEnabled(
            ready
            and self.status is not None
            and (not active or self.status.state == "paused")
        )
        self.stop_button.setEnabled(ready and active)
        self.open_button.setEnabled(ready and active)
        self.configure_button.setEnabled(
            ready and self.status is not None and not active
        )
        self.dashboard_start.setEnabled(self.start_button.isEnabled())
        self.dashboard_stop.setEnabled(self.stop_button.isEnabled())
        self.dashboard_open.setEnabled(self.open_button.isEnabled())
        selected = self._selection()
        if selected is not None and selected.phase != "ready":
            selected = None
        can_start = (
            self.session_checked and self.session is None and selected is not None
        )
        if self.session is not None:
            session_vm = self.session.vm_name
        elif selected is not None:
            session_vm = selected.name
        else:
            session_vm = "Select a VM on the Virtual Machines page."
        self.session_vm.setText(session_vm)
        self.session_choose_vm.setEnabled(ready and self.session is None)
        self.session_start.setEnabled(ready and can_start)
        self.session_network.setEnabled(ready and can_start)
        self.session_profile.setEnabled(ready and can_start)
        self.session_duration.setEnabled(
            ready
            and can_start
            and self.session_profile.currentText() == PROFILES["contest"].name
        )
        self.session_cage.setEnabled(ready and can_start)
        self.session_reopen.setEnabled(
            ready and self.session is not None and self.session.state == "disconnected"
        )
        self.session_end.setEnabled(ready and self.session is not None)
        self.session_refresh.setEnabled(ready)

    def _load_settings(self) -> None:
        def load() -> Settings | None:
            try:
                return self.store.load()
            except FileNotFoundError:
                return None

        def loaded(settings: Settings | None) -> None:
            if settings is not None:
                self._show_settings(settings)
                self._refresh()
            elif self.show_wizard:
                self._open_setup()
            else:
                self._refresh()

        self._submit("Loading settings…", load, loaded)

    def _open_setup(self) -> None:
        if self.wizard is not None:
            return
        wizard = SetupWizard(self.paths, self)
        self.wizard = wizard
        wizard.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

        def accepted() -> None:
            settings = Settings.for_pool(wizard.storage_pool)

            def initialize() -> Settings:
                self.store.initialize(settings)
                return self.store.load()

            def initialized(saved: Settings) -> None:
                self._show_settings(saved)
                self._refresh()

            self._submit("Saving initial settings…", initialize, initialized)

        def finished(_result: int) -> None:
            self.wizard = None
            self._sync_actions()

        wizard.accepted.connect(accepted)
        wizard.finished.connect(finished)
        self._sync_actions()
        wizard.open()

    def _show_settings(self, settings: Settings) -> None:
        self.settings = settings
        application = settings.application
        self.session_profile.setCurrentText(PROFILES[application.default_profile].name)
        self.pool_input.setText(application.storage_pool)
        self.theme_input.setCurrentIndex(THEMES.index(application.theme))
        self.cpu_input.setText(str(settings.vm_defaults.cpus))
        self.memory_input.setText(str(settings.vm_defaults.memory_mib))
        self.disk_input.setText(str(settings.vm_defaults.disk_gib))
        self._default_options(application.default_vm)
        color = {
            "system": Qt.ColorScheme.Unknown,
            "light": Qt.ColorScheme.Light,
            "dark": Qt.ColorScheme.Dark,
        }[application.theme]
        QGuiApplication.styleHints().setColorScheme(color)
        self._sync_actions()

    def _default_options(self, selected: UUID | None) -> None:
        self.default_vm.clear()
        self.default_choices = [None]
        self.default_vm.addItem("Automatic")
        for record in self.records:
            if record.phase == "ready":
                self.default_choices.append(record.uuid)
                self.default_vm.addItem(record.name)
        if selected is not None and selected not in self.default_choices:
            self.default_choices.append(selected)
            self.default_vm.addItem(f"Unavailable VM ({selected})")
        self.default_vm.setCurrentIndex(self.default_choices.index(selected))

    def _save_settings(self) -> None:
        baseline = self.settings
        if baseline is None:
            return
        theme: Theme = THEMES[self.theme_input.currentIndex()]
        selection = self.default_choices[self.default_vm.currentIndex()]
        values = baseline.vm_defaults
        try:
            application = replace(
                baseline.application,
                storage_pool=self.pool_input.text(),
                theme=theme,
                default_vm=selection,
            )
            defaults = VMDefaults(
                cpus=positive_value(self.cpu_input, "default vCPUs"),
                memory_mib=positive_value(self.memory_input, "default memory"),
                disk_gib=positive_value(self.disk_input, "default disk capacity"),
            )
        except ConfigError as error:
            self._error(str(error))
            return

        def update(current: Settings) -> Settings:
            return Settings(
                replace(
                    current.application,
                    storage_pool=application.storage_pool
                    if application.storage_pool != baseline.application.storage_pool
                    else current.application.storage_pool,
                    theme=theme
                    if theme != baseline.application.theme
                    else current.application.theme,
                    default_vm=selection
                    if selection != baseline.application.default_vm
                    else current.application.default_vm,
                ),
                VMDefaults(
                    cpus=defaults.cpus
                    if defaults.cpus != values.cpus
                    else current.vm_defaults.cpus,
                    memory_mib=defaults.memory_mib
                    if defaults.memory_mib != values.memory_mib
                    else current.vm_defaults.memory_mib,
                    disk_gib=defaults.disk_gib
                    if defaults.disk_gib != values.disk_gib
                    else current.vm_defaults.disk_gib,
                ),
            )

        def saved(settings: Settings) -> None:
            self._show_settings(settings)
            self.task_status.setText("Settings saved.")

        self._submit("Saving settings…", lambda: self.store.update(update), saved)

    def _check_host(self) -> None:
        def check() -> list[Diagnostic]:
            try:
                pool = self.store.load().application.storage_pool
            except FileNotFoundError:
                pool = None
            return diagnose(self.paths, pool)

        def show(checks: list[Diagnostic]) -> None:
            self.host_results.setPlainText(
                "\n".join(
                    f"{item.status.upper()}  {item.name.replace('_', ' ').capitalize()}\n{item.detail}\n"
                    for item in checks
                )
            )

        self._submit("Checking the host…", check, show)

    def _refresh(self) -> None:
        self._refresh_inventory()

    def _refresh_inventory(self, preferred: UUID | None = None) -> None:
        selected = self._selection()
        target = preferred or (selected.uuid if selected else None)
        if target is None and self.settings is not None:
            target = self.settings.application.default_vm

        def loaded(records: tuple[VMRecord, ...]) -> None:
            old_index = self.default_vm.currentIndex()
            default = self.default_choices[old_index] if old_index >= 0 else None
            self.records = records
            self._default_options(default)
            self.vm_list.blockSignals(True)
            self.vm_list.clear()
            row = 0 if records else -1
            for index, record in enumerate(records):
                item = QListWidgetItem(
                    record.name
                    if record.phase == "ready"
                    else f"{record.name} ({record.phase})"
                )
                item.setToolTip(str(record.uuid))
                self.vm_list.addItem(item)
                if record.uuid == target:
                    row = index
            self.vm_list.setCurrentRow(row)
            self.vm_list.blockSignals(False)
            self._selection_changed(row)

        self._submit("Refreshing virtual machines…", self.service.list_records, loaded)

    def _selection(self) -> VMRecord | None:
        row = self.vm_list.currentRow()
        return self.records[row] if 0 <= row < len(self.records) else None

    def _selection_changed(self, _row: int) -> None:
        self.status = None
        selected = self._selection()
        if selected is None:
            self._set_vm_text("Create a VM to get started.")
        elif selected.phase == "abandoned":
            self._set_vm_text(
                "This creation attempt was abandoned. Its storage records were preserved."
            )
        elif selected.phase == "pending":
            self._set_vm_text(
                "Creation was interrupted. Retry with the same name and image."
            )
        else:
            self._set_vm_text("Reading VM status…")
            self._submit(
                "Reading VM status…",
                lambda: self.service.status(selected.uuid),
                self._show_status,
                failure=self._status_error,
            )
        self._sync_actions()

    def _poll_selected(self) -> None:
        if self.pages.currentIndex() == _SESSIONS_PAGE:
            self._poll_session()
            return
        selected = self._selection()
        if (
            self.tasks.busy
            or self.wizard is not None
            or self.pages.currentIndex() == _SETTINGS_PAGE
            or QApplication.activeModalWidget() is not None
            or selected is None
            or selected.phase != "ready"
        ):
            return

        def polled(status: VMStatus) -> None:
            self.polling = False
            self._show_status(status)
            self._poll_session_if_needed()

        def failed(message: str) -> None:
            self.polling = False
            self._status_error(message)
            self._poll_session_if_needed()

        self.polling = self.tasks.submit(
            "Reading VM status…",
            lambda: self.service.status(selected.uuid),
            polled,
            failed,
        )
        self._sync_actions()

    def _poll_session(self) -> None:
        if (
            self.tasks.busy
            or self.wizard is not None
            or QApplication.activeModalWidget() is not None
        ):
            return
        self.tasks.submit(
            "Reading focus session status…",
            self.session_service.status,
            self._show_session,
            self._session_status_error,
        )
        self._sync_actions()

    def _show_session(self, session: FocusSession | None) -> None:
        self.session = session
        self.session_checked = True
        if session is None:
            self.session_status.setText("No focus session is running.")
        else:
            started = datetime.fromisoformat(session.started_at)
            elapsed_seconds = max(0, int((datetime.now(UTC) - started).total_seconds()))
            elapsed = timedelta(seconds=elapsed_seconds)
            network = session.network.capitalize()
            if session.state != "unresolved":
                network = f"{network} (offline)"
            profile_name = next(
                (
                    profile.name
                    for key, profile in PROFILES.items()
                    if key == session.profile
                ),
                session.profile,
            )
            status = (
                f"{session.vm_name} · {session.state.capitalize()}\n"
                f"{profile_name} · {network} · {elapsed} elapsed"
            )
            if (remaining := session.remaining_seconds()) is not None:
                timer = str(timedelta(seconds=remaining))
                status += (
                    f"\n{timer} remaining" if remaining else "\nContest timer expired."
                )
            if session.cage:
                status += "\nCage viewer"
            if session.detail:
                status = f"{status}\n{session.detail}"
            self.session_status.setText(status)
        self._sync_actions()

    def _session_status_error(self, message: str) -> None:
        self.session = None
        self.session_checked = False
        self.session_status.setText(f"Focus session status is unavailable. {message}")
        self._sync_actions()

    def _session_action_error(self, message: str) -> None:
        self._error(message)
        self._poll_session()

    def _start_session(self) -> None:
        selected = self._selection()
        if selected is None or not self.session_start.isEnabled():
            return
        network = OFFLINE_NETWORKS[self.session_network.currentIndex()]
        profile = tuple(PROFILES)[self.session_profile.currentIndex()]
        duration = (
            (self.session_duration.value() or None) if profile == "contest" else None
        )
        cage = self.session_cage.isChecked()
        self._submit(
            "Starting focus session…",
            lambda: self.session_service.start(
                selected.uuid, profile, network, duration_minutes=duration, cage=cage
            ),
            self._show_session,
            failure=self._session_action_error,
        )

    def _set_profile_options(self, index: int) -> None:
        profile = PROFILES[tuple(PROFILES)[index]]
        self.session_network.setCurrentIndex(OFFLINE_NETWORKS.index(profile.network))
        self._sync_actions()

    def _reopen_session(self) -> None:
        self._submit(
            "Reopening focus viewer…",
            self.session_service.reopen,
            self._show_session,
            failure=self._session_action_error,
        )

    def _end_session(self) -> None:
        def ended(_result: None) -> None:
            self._show_session(None)
            self.task_status.setText("Focus session ended.")

        self._submit(
            "Ending focus session…",
            self.session_service.end,
            ended,
            failure=self._session_action_error,
        )

    def _status_error(self, message: str) -> None:
        self.status = None
        self._set_vm_text(f"VM status is unavailable. {message}")
        if self.task_status.text() == "Reading VM status…":
            self.task_status.clear()
        self._sync_actions()

    def _set_vm_text(self, text: str) -> None:
        self.vm_status.setText(text)
        self.dashboard_status.setText(text)

    def _show_status(self, status: VMStatus) -> None:
        self.status = status
        self._set_vm_text(
            f"{status.name} · {status.state.replace('-', ' ').capitalize()}\n"
            f"{status.cpus} vCPUs · {status.memory_mib:,} MiB RAM\n"
            f"Disk {status.disk_capacity_bytes / GIB:g} GiB virtual · {status.disk_allocation_bytes / GIB:.2f} GiB allocated"
        )
        self._sync_actions()

    def _create(self) -> None:
        settings = self.settings
        if settings is None:
            return
        dialog = CreateVMDialog(settings.vm_defaults, self)
        dialog.exec()
        request = dialog.request
        dialog.request = None
        dialog.deleteLater()
        if request is None:
            return

        def create() -> VMStatus:
            pool = self.store.load().application.storage_pool
            return self.service.create(
                request.name, request.source, request.password, pool, request.resources
            )

        self._submit(
            "Verifying the image and creating the VM…",
            create,
            lambda status: self._refresh_inventory(status.uuid),
        )

    def _start(self) -> None:
        if (selected := self._selection()) is not None:
            self._submit(
                "Starting VM…",
                lambda: self.service.start(selected.uuid),
                self._show_status,
            )

    def _stop(self) -> None:
        if (selected := self._selection()) is not None:
            self._submit(
                "Waiting for guest shutdown…",
                lambda: self.service.stop(selected.uuid, 60),
                self._show_status,
            )

    def _configure(self) -> None:
        status = self.status
        if status is None:
            return
        dialog = ResourceDialog(status.cpus, status.memory_mib, self)
        dialog.exec()
        resources = dialog.resources
        dialog.deleteLater()
        if resources is not None:
            self._submit(
                "Updating VM resources…",
                lambda: self.service.configure(status.uuid, *resources),
                self._show_status,
            )

    def _open_console(self) -> None:
        if (selected := self._selection()) is None:
            return

        def prepare() -> tuple[str, list[str]]:
            self.service.check_console(selected.uuid)
            return console_command(selected.uuid)

        def open_viewer(command: tuple[str, list[str]]) -> None:
            started, _pid = QProcess.startDetached(*command)
            self.task_status.setText(
                "Viewer launched." if started else "Could not launch virt-viewer."
            )

        self._submit("Opening the VM console…", prepare, open_viewer)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.tasks.busy or self.resources_page.tasks.busy or self.wizard is not None:
            self.task_status.setText(
                "Wait for the current operation or finish setup before closing."
            )
            event.ignore()
            return
        super().closeEvent(event)

    def _refresh_resources_page(self, row: int) -> None:
        if row == _RESOURCES_PAGE:
            self.resources_page.refresh()
