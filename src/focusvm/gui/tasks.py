"""Run one background operation at a time and deliver results on the Qt thread."""

import logging
from collections.abc import Callable

from PySide6.QtCore import QObject, Qt, QThread, Slot

from ..config import ConfigError
from ..local_state import PrivateFileError
from ..provisioning import ProvisioningError, ProvisioningUnavailable
from ..resources import ResourceError, ResourceUnavailable
from ..sessions import SessionError, SessionInputError
from ..viewer import ViewerError
from ..virtualization import VirtualizationError
from ..vm import VMInputError, VMStateError

_LOG = logging.getLogger(__name__)
_EXPECTED_ERRORS = (
    ConfigError,
    OSError,
    PrivateFileError,
    ProvisioningError,
    ProvisioningUnavailable,
    ResourceError,
    ResourceUnavailable,
    SessionError,
    SessionInputError,
    ViewerError,
    VirtualizationError,
    VMInputError,
    VMStateError,
)


class _Worker(QThread):
    def __init__(
        self,
        parent: QObject,
        operation: Callable[[], Callable[[], None]],
        failure: Callable[[str], None],
    ) -> None:
        super().__init__(parent)
        self.operation = operation
        self.failure = failure
        self.deliver: Callable[[], None] | None = None

    def run(self) -> None:
        try:
            self.deliver = self.operation()
        except _EXPECTED_ERRORS as error:
            self._report(str(error))
        except Exception:
            _LOG.exception("Unexpected error in a FocusVM background operation")
            self._report(
                "An unexpected error occurred. Check the terminal for details."
            )

    def _report(self, message: str) -> None:
        self.deliver = lambda: self.failure(message)


class TaskRunner(QObject):
    def __init__(self, parent: QObject) -> None:
        super().__init__(parent)
        self._worker: _Worker | None = None

    @property
    def busy(self) -> bool:
        return self._worker is not None

    def submit[T](
        self,
        label: str,
        operation: Callable[[], T],
        on_success: Callable[[T], None],
        on_error: Callable[[str], None],
    ) -> bool:
        if self.busy:
            return False

        def execute() -> Callable[[], None]:
            result = operation()
            return lambda: on_success(result)

        worker = _Worker(self, execute, on_error)
        worker.setObjectName(label)
        self._worker = worker
        worker.finished.connect(self._complete, Qt.ConnectionType.QueuedConnection)
        worker.start()
        return True

    @Slot()
    def _complete(self) -> None:
        worker = self._worker
        if worker is None:
            return
        self._worker = None
        deliver = worker.deliver
        worker.deleteLater()
        if deliver is not None:
            deliver()
