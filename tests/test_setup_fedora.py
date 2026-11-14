"""Test the Fedora helper without changing host services or libvirt."""

# pyright: reportPrivateUsage=false

from pathlib import Path
from unittest.mock import patch

import pytest

from scripts import setup_fedora


def test_modular_setup_leaves_legacy_daemon_running() -> None:
    def unit_has_state(state: str, unit: str) -> bool:
        return (state, unit) == ("is-active", "libvirtd.service")

    with (
        patch.object(
            setup_fedora,
            "_unit_has_state",
            side_effect=unit_has_state,
        ),
        patch.object(setup_fedora, "_run") as run,
    ):
        setup_fedora._services()
    run.assert_not_called()


def test_enables_required_modular_sockets_on_a_fresh_host() -> None:
    with (
        patch.object(setup_fedora, "_unit_has_state", return_value=False),
        patch.object(setup_fedora, "_run") as run,
    ):
        setup_fedora._services()
    run.assert_called_once_with(
        "/usr/bin/systemctl",
        "enable",
        "--now",
        "virtqemud.socket",
        "virtqemud-ro.socket",
        "virtstoraged.socket",
        "virtstoraged-ro.socket",
    )


def test_pool_refuses_existing_mismatched_definition(tmp_path: Path) -> None:
    target = tmp_path / "focusvm"
    commands: list[tuple[str, ...]] = []

    def virsh(*args: str) -> str:
        commands.append(args)
        if args[0] == "pool-list":
            return "focusvm\n"
        assert args[0] == "pool-dumpxml"
        return '<pool type="dir"><target><path>/other</path></target></pool>'

    with (
        patch.object(setup_fedora, "_virsh", side_effect=virsh),
        patch.object(setup_fedora, "_run") as run,
        pytest.raises(setup_fedora.SetupError, match="another type or path"),
    ):
        setup_fedora._pool(target)
    assert not target.exists()
    run.assert_not_called()
    assert all(command[0] in {"pool-list", "pool-dumpxml"} for command in commands)


def test_new_pool_creates_directory_and_starts(tmp_path: Path) -> None:
    target = tmp_path / "focusvm"
    with (
        patch.object(setup_fedora, "_virsh", return_value="") as virsh,
        patch.object(setup_fedora, "_run") as run,
    ):
        setup_fedora._pool(target)
    assert target.is_dir()
    assert target.stat().st_mode & 0o022 == 0
    run.assert_called_once_with("/usr/bin/restorecon", str(target))
    assert [call.args for call in virsh.call_args_list] == [
        ("pool-list", "--all", "--name"),
        ("pool-define-as", "focusvm", "dir", "--target", str(target)),
        ("pool-list", "--name"),
        ("pool-start", "focusvm"),
        ("pool-autostart", "focusvm"),
    ]


def test_pool_refuses_symlink_without_querying_host(tmp_path: Path) -> None:
    directory = tmp_path / "other"
    directory.mkdir()
    link = tmp_path / "focusvm"
    link.symlink_to(directory, target_is_directory=True)
    with (
        patch.object(setup_fedora, "_virsh") as virsh,
        pytest.raises(setup_fedora.SetupError, match="real pool directory"),
    ):
        setup_fedora._pool(link)
    virsh.assert_not_called()
