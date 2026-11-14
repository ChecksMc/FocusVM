# Exercise the installer using its temporary Python environments and launcher boundary.
# pyright: reportPrivateUsage=false

import subprocess
from pathlib import Path

import pytest

from scripts import install as installer
from scripts.install import InstallError, Layout, install, uninstall


@pytest.fixture
def layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Layout:
    def fake_build(release: Path, _source: Path, *, gui: bool) -> None:
        (release / "launch").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

    monkeypatch.setattr(installer, "_build_release", fake_build)
    return Layout(tmp_path / "data with space", tmp_path / "home with space/.local/bin")


def test_install_upgrade_and_uninstall_preserve_user_data(layout: Layout) -> None:
    references = layout.data / "focusvm/resources"
    references.mkdir(parents=True)
    reference = references / "notes.txt"
    reference.write_text("my notes", encoding="utf-8")

    install(layout, Path.cwd(), gui=True)
    previous = layout.current.resolve()
    assert layout.launcher.resolve() == previous / "launch"
    assert f'Exec=/bin/sh "{layout.launcher}" gui' in layout.desktop.read_text(
        encoding="utf-8"
    )

    install(layout, Path.cwd(), gui=False)
    assert layout.launcher.resolve().parent != previous
    assert not previous.exists()
    assert not layout.desktop.exists()
    assert not layout.desktop.is_symlink()

    assert uninstall(layout)
    assert not uninstall(layout)
    assert not layout.root.exists()
    assert not layout.launcher.is_symlink()
    assert reference.read_text(encoding="utf-8") == "my notes"


def test_uninstall_does_not_remove_replacement_launcher(layout: Layout) -> None:
    install(layout, Path.cwd(), gui=True)
    layout.launcher.unlink()
    layout.launcher.write_text("unrelated executable", encoding="utf-8")
    layout.desktop.unlink()
    layout.desktop.write_text("unrelated desktop entry", encoding="utf-8")

    assert uninstall(layout)
    assert layout.launcher.read_text(encoding="utf-8") == "unrelated executable"
    assert layout.desktop.read_text(encoding="utf-8") == "unrelated desktop entry"
    assert not layout.root.exists()


def test_uninstall_leaves_unmanaged_release_directory(layout: Layout) -> None:
    install(layout, Path.cwd(), gui=False)
    unrelated = layout.releases / ("1" * 32)
    unrelated.mkdir()
    keep = unrelated / "keep.txt"
    keep.write_text("unrelated file", encoding="utf-8")
    assert uninstall(layout)
    assert keep.read_text(encoding="utf-8") == "unrelated file"
    assert set(layout.releases.iterdir()) == {unrelated}
    assert not layout.current.is_symlink()


def test_install_refuses_unrelated_launcher_and_unmanaged_directory(
    layout: Layout,
) -> None:
    layout.launcher.parent.mkdir(parents=True)
    layout.launcher.symlink_to("another-program")
    with pytest.raises(InstallError, match="Another program already uses"):
        install(layout, Path.cwd(), gui=True)
    assert layout.launcher.readlink() == Path("another-program")
    assert not layout.root.exists()

    layout.launcher.unlink()
    layout.root.mkdir()
    unrelated = layout.root / "keep.txt"
    unrelated.touch()
    with pytest.raises(InstallError, match="another installation"):
        install(layout, Path.cwd(), gui=True)
    with pytest.raises(InstallError, match="another installation"):
        uninstall(layout)
    assert unrelated.is_file()


def test_failed_upgrade_keeps_existing_installation(
    layout: Layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(layout, Path.cwd(), gui=True)
    original = layout.current.resolve()
    desktop = layout.desktop.read_bytes()

    def failed_build(release: Path, _source: Path, *, gui: bool) -> None:
        (release / "partial").touch()
        raise subprocess.CalledProcessError(1, "pip")

    monkeypatch.setattr(installer, "_build_release", failed_build)
    with pytest.raises(subprocess.CalledProcessError):
        install(layout, Path.cwd(), gui=False)
    assert layout.current.resolve() == original
    assert layout.launcher.resolve() == original / "launch"
    assert layout.desktop.read_bytes() == desktop
    assert set(layout.releases.iterdir()) == {original}


def test_failure_registering_desktop_entry_rolls_back_switch(
    layout: Layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(layout, Path.cwd(), gui=False)
    original = layout.current.resolve()
    symlink = Path.symlink_to

    def deny_menu(path: Path, target: Path, target_is_directory: bool = False) -> None:
        if path == layout.desktop:
            raise PermissionError("desktop entry denied")
        symlink(path, target, target_is_directory)

    monkeypatch.setattr(Path, "symlink_to", deny_menu)
    with pytest.raises(PermissionError, match="desktop entry denied"):
        install(layout, Path.cwd(), gui=True)
    assert layout.current.resolve() == original
    assert layout.launcher.resolve() == original / "launch"
    assert set(layout.releases.iterdir()) == {original}


def test_upgrade_preserves_running_application_and_uninstall_can_retry(
    layout: Layout,
) -> None:
    install(layout, Path.cwd(), gui=True)
    old = layout.current.resolve()
    with installer._locked_directory(old, shared=True):
        install(layout, Path.cwd(), gui=False)
        assert old.exists()
        with pytest.raises(InstallError, match="Close FocusVM"):
            uninstall(layout)
        assert layout.launcher.is_symlink()
    assert uninstall(layout)
    assert not old.exists()


def test_layout_honors_absolute_xdg_and_ignores_relative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "elsewhere"))
    assert Layout.from_environment() == Layout(
        tmp_path / "elsewhere", tmp_path / ".local/bin"
    )
    monkeypatch.setenv("XDG_DATA_HOME", "relative")
    assert Layout.from_environment() == Layout(
        tmp_path / ".local/share", tmp_path / ".local/bin"
    )
