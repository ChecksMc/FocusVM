"""Optional Qt desktop manager. The CLI imports Qt only for the gui command."""

import os

from ..config import ConfigPaths


class GUIUnavailable(RuntimeError):
    """The desktop manager cannot launch in the current environment."""


def launch(paths: ConfigPaths) -> int:
    try:
        from PySide6.QtWidgets import QApplication
    except (ImportError, OSError) as error:
        raise GUIUnavailable(
            "Install the desktop dependencies with pip install 'focusvm[gui]'"
        ) from error

    if not any(
        os.environ.get(name)
        for name in ("WAYLAND_DISPLAY", "WAYLAND_SOCKET", "DISPLAY", "QT_QPA_PLATFORM")
    ):
        raise GUIUnavailable("Launch FocusVM from a graphical session")

    from .window import MainWindow

    app = QApplication(["focusvm"])
    app.setApplicationName("FocusVM")
    window = MainWindow(paths)
    window.show()
    return app.exec()
