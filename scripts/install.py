"""Install and remove the per-user FocusVM application from a source checkout."""

import argparse
import fcntl
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import venv
from collections.abc import Generator, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

_RECEIPT = ".focusvm-install-v1"
_MARKER = b"FocusVM per-user install v1\n"
_DESKTOP = "io.github.ChecksMc.FocusVM.desktop"
_RELEASE_NAME = re.compile(r"[0-9a-f]{32}")
_CHECK_QT = (
    "import PySide6; from PySide6.QtWidgets import QApplication; "
    "raise SystemExit(PySide6.__version_info__[:2] < (6, 8))"
)


class InstallError(RuntimeError):
    """A managed installation could not be changed safely."""


@dataclass(frozen=True, slots=True)
class Layout:
    data: Path
    bin: Path

    @classmethod
    def from_environment(cls) -> "Layout":
        home = Path.home().resolve()
        data = Path(os.environ.get("XDG_DATA_HOME", ""))
        if not data.is_absolute():
            data = home / ".local/share"
        return cls(data.resolve(), home / ".local/bin")

    @property
    def root(self) -> Path:
        return self.data / "focusvm-install"

    @property
    def current(self) -> Path:
        return self.root / "current"

    @property
    def releases(self) -> Path:
        return self.root / "releases"

    @property
    def launcher(self) -> Path:
        return self.bin / "focusvm"

    @property
    def desktop(self) -> Path:
        return self.data / "applications" / _DESKTOP


def install(layout: Layout, source: Path, *, gui: bool) -> None:
    layout.data.mkdir(mode=0o700, parents=True, exist_ok=True)
    with _locked_directory(layout.data):
        _ensure_shared_parent(layout.launcher, create=True)
        _ensure_shared_parent(layout.desktop, create=gui)
        launcher_target = layout.current / "launch"
        desktop_target = layout.current / _DESKTOP
        _check_output(layout.launcher, launcher_target)
        if gui:
            _check_output(layout.desktop, desktop_target)
        first_install = not _owned(layout.root)
        if first_install:
            _mark(layout.root)
        layout.releases.mkdir(mode=0o700, exist_ok=True)
        _check_directory(layout.releases)
        previous = _current_target(layout)
        release = layout.releases / uuid4().hex
        _mark(release)
        try:
            _build_release(release, source, gui=gui)
            if gui:
                (release / _DESKTOP).write_text(
                    _desktop_entry(layout.launcher), encoding="utf-8"
                )
            _publish(layout, release, previous, gui=gui)
        except BaseException:
            if not _matches_link(layout.current, release.relative_to(layout.root)):
                shutil.rmtree(release)
                if first_install:
                    _remove_empty_installation(layout)
            raise
        _prune(layout, keep=release)


def uninstall(layout: Layout) -> bool:
    if not os.path.lexists(layout.root):
        return False
    with _locked_directory(layout.data), ExitStack() as locks:
        if not _owned(layout.root):
            return False
        _current_target(layout)
        releases = _owned_releases(layout)
        for release in releases:
            try:
                locks.enter_context(_locked_directory(release, blocking=False))
            except BlockingIOError as error:
                raise InstallError(
                    "Close FocusVM windows and commands before uninstalling."
                ) from error
        outputs = [
            path
            for path, target in (
                (layout.launcher, layout.current / "launch"),
                (layout.desktop, layout.current / _DESKTOP),
            )
            if _ensure_shared_parent(path, create=False) and _matches_link(path, target)
        ]
        for path in outputs:
            path.unlink()
        layout.current.unlink(missing_ok=True)
        for release in releases:
            shutil.rmtree(release)
        _remove_empty_installation(layout)
        return True


def _build_release(release: Path, source: Path, *, gui: bool) -> None:
    venv.EnvBuilder(with_pip=True, system_site_packages=True).create(release)
    python = release / "bin/python"
    qt_available = (
        gui
        and subprocess.run(  # noqa: S603 - Fixed commands in the new virtual environment.
            [python, "-I", "-c", _CHECK_QT],
            capture_output=True,
            check=False,
        ).returncode
        == 0
    )
    package = ".[gui]" if gui and not qt_available else "."
    subprocess.run(  # noqa: S603 - Local source distribution and its declared dependencies.
        [
            python,
            "-I",
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--ignore-installed",
            "--no-input",
            package,
        ],
        cwd=source,
        check=True,
    )
    subprocess.run([python, "-I", "-m", "focusvm", "--version"], check=True)  # noqa: S603
    if gui:
        subprocess.run(  # noqa: S603 - Verify imports before publishing the menu entry.
            [python, "-I", "-c", _CHECK_QT],
            check=True,
        )
    manager = release / "installer.py"
    shutil.copyfile(__file__, manager)
    launch = release / "launch"
    command = shlex.join([str(python), "-I", str(manager), "launch"])
    launch.write_text(f'#!/bin/sh\nexec {command} "$@"\n', encoding="utf-8")
    launch.chmod(0o700)


def _publish(
    layout: Layout, release: Path, previous: Path | None, *, gui: bool
) -> None:
    target = release.relative_to(layout.root)
    staged = release / "current-link"
    staged.symlink_to(target)
    old_desktop = _matches_link(layout.desktop, layout.current / _DESKTOP)
    launcher_added = False
    desktop_added = False
    try:
        os.replace(staged, layout.current)
        if not _matches_link(layout.launcher, layout.current / "launch"):
            layout.launcher.symlink_to(layout.current / "launch")
            launcher_added = True
        if gui and not old_desktop:
            layout.desktop.symlink_to(layout.current / _DESKTOP)
            desktop_added = True
        if not gui and old_desktop:
            layout.desktop.unlink()
    except BaseException:
        if desktop_added:
            layout.desktop.unlink(missing_ok=True)
        if launcher_added:
            layout.launcher.unlink(missing_ok=True)
        if _matches_link(layout.current, target):
            if previous is None:
                layout.current.unlink()
            else:
                staged.symlink_to(previous)
                os.replace(staged, layout.current)
        raise


def _desktop_entry(launcher: Path) -> str:
    # The desktop string and Exec argument each have their own escape pass.
    path = str(launcher)
    if any(character in path for character in "\n\r\t"):
        raise InstallError(
            "Use a home directory without tabs or line breaks for the desktop launcher."
        )
    argument = "".join(f"\\{char}" if char in '\\"$`' else char for char in path)
    argument = argument.replace("\\", "\\\\").replace("%", "%%")
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=FocusVM\n"
        "Comment=Manage offline development VMs\n"
        f'Exec=/bin/sh "{argument}" gui\n'
        "Terminal=false\n"
        "Categories=Development;\n"
    )


def _current_target(layout: Layout) -> Path | None:
    if not os.path.lexists(layout.current):
        return None
    if not layout.current.is_symlink():
        raise InstallError(f"Expected a managed installation link at {layout.current}.")
    target = layout.current.readlink()
    if (
        target.parent != Path("releases")
        or not _RELEASE_NAME.fullmatch(target.name)
        or not _owned(layout.root / target)
    ):
        raise InstallError(
            f"Unexpected FocusVM installation target at {layout.current}."
        )
    return target


def _owned_releases(layout: Layout) -> list[Path]:
    if not os.path.lexists(layout.releases):
        return []
    _check_directory(layout.releases)
    return [
        path
        for path in layout.releases.iterdir()
        if _RELEASE_NAME.fullmatch(path.name)
        and not path.is_symlink()
        and os.path.lexists(path / _RECEIPT)
        and _owned(path)
    ]


def _prune(layout: Layout, *, keep: Path) -> None:
    for release in _owned_releases(layout):
        if release != keep:
            try:
                with _locked_directory(release, blocking=False):
                    shutil.rmtree(release)
            except BlockingIOError:
                pass


def _remove_empty_installation(layout: Layout) -> None:
    if layout.releases.is_dir() and not any(layout.releases.iterdir()):
        layout.releases.rmdir()
    if set(layout.root.iterdir()) == {layout.root / _RECEIPT}:
        (layout.root / _RECEIPT).unlink()
        layout.root.rmdir()


def _mark(path: Path) -> None:
    path.mkdir(mode=0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    with os.fdopen(os.open(path / _RECEIPT, flags, 0o600), "wb") as marker:
        marker.write(_MARKER)


def _owned(path: Path) -> bool:
    if not os.path.lexists(path):
        return False
    _check_directory(path)
    marker = path / _RECEIPT
    try:
        details = marker.lstat()
    except FileNotFoundError:
        raise InstallError(
            f"Existing directory belongs to another installation: {path}."
        ) from None
    if (
        not stat.S_ISREG(details.st_mode)
        or details.st_uid != os.getuid()
        or details.st_mode & 0o022
        or details.st_nlink != 1
        or marker.read_bytes() != _MARKER
    ):
        raise InstallError(f"Invalid FocusVM installation receipt at {marker}.")
    return True


def _check_output(path: Path, target: Path) -> None:
    if os.path.lexists(path) and not _matches_link(path, target):
        raise InstallError(
            f"Another program already uses {path}. Move it before installing FocusVM."
        )


def _matches_link(path: Path, target: Path) -> bool:
    try:
        return path.is_symlink() and path.readlink() == target
    except FileNotFoundError:
        return False


def _ensure_shared_parent(path: Path, *, create: bool) -> bool:
    parent = path.parent
    if create:
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not os.path.lexists(parent):
        return False
    _check_directory(parent)
    return True


def _check_directory(path: Path) -> None:
    _check_directory_mode(path, path.lstat())


def _check_directory_mode(path: Path, details: os.stat_result) -> None:
    if (
        not stat.S_ISDIR(details.st_mode)
        or details.st_uid != os.getuid()
        or details.st_mode & 0o022
    ):
        raise InstallError(
            f"Expected a directory owned by you and protected from other writers: {path}."
        )


@contextmanager
def _locked_directory(
    path: Path, *, blocking: bool = True, shared: bool = False
) -> Generator[int]:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        _check_directory_mode(path, os.fstat(descriptor))
        operation = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
        if not blocking:
            operation |= fcntl.LOCK_NB
        fcntl.flock(descriptor, operation)
        yield descriptor
    finally:
        os.close(descriptor)


def _launch(arguments: Sequence[str]) -> int:
    release = Path(__file__).resolve().parent
    if release.parent.name != "releases" or not _RELEASE_NAME.fullmatch(release.name):
        raise InstallError("Run the managed FocusVM launcher from the installation.")
    data = release.parents[2]
    if arguments[:1] == ["uninstall"]:
        parser = argparse.ArgumentParser(
            prog="focusvm uninstall", description="Remove the user-installed app."
        )
        parser.parse_args(arguments[1:])
        return _uninstall(Layout(data, Layout.from_environment().bin))
    with _locked_directory(release, shared=True) as descriptor:
        os.set_inheritable(descriptor, True)
        os.execv(  # noqa: S606 - Execute the selected Python with separate CLI arguments.
            sys.executable, [sys.executable, "-I", "-m", "focusvm", *arguments]
        )


def _uninstall(layout: Layout) -> int:
    if uninstall(layout):
        print(
            "Removed the FocusVM application and its launchers. VM storage and user data were preserved."
        )
    else:
        print("No per-user FocusVM installation found.")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            raise InstallError(
                "Run the FocusVM installer as your regular desktop user."
            )
        if arguments[:1] == ["launch"]:
            return _launch(arguments[1:])
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("action", choices=("install", "uninstall"))
        parser.add_argument(
            "--cli-only",
            action="store_true",
            help="Install without the desktop manager.",
        )
        args = parser.parse_args(arguments)
        layout = Layout.from_environment()
        if args.action == "uninstall":
            if args.cli_only:
                parser.error("--cli-only applies to install")
            return _uninstall(layout)
        if sys.version_info < (3, 12):  # noqa: UP036 - The installer can run before FocusVM is installed.
            raise InstallError("FocusVM requires Python 3.12 or newer.")
        install(layout, Path(__file__).resolve().parent.parent, gui=not args.cli_only)
        print(
            f"Installed FocusVM at {layout.launcher}. Run focusvm setup to get started."
        )
        if str(layout.bin) not in os.get_exec_path():
            print(f"Add {layout.bin} to PATH or run the launcher by its full path.")
        return 0
    except subprocess.CalledProcessError as error:
        print(
            f"FocusVM installation failed during a package or import check (exit {error.returncode}).",
            file=sys.stderr,
        )
    except (InstallError, OSError) as error:
        print(f"FocusVM installer: {error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
