"""Prepare system libvirt and a dedicated storage pool on Fedora."""

import os
import stat
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from xml.etree import ElementTree

POOL = "focusvm"
TARGET = Path("/var/lib/libvirt/images/focusvm")
URI = "qemu:///system"


class SetupError(RuntimeError):
    pass


def _run(*args: str) -> str:
    return subprocess.run(  # noqa: S603 - Fixed tools and validated local identifiers.
        args, check=True, text=True, stdout=subprocess.PIPE
    ).stdout


def _unit_has_state(state: str, unit: str) -> bool:
    return (
        subprocess.run(  # noqa: S603 - Only fixed unit names are passed by the caller.
            ["/usr/bin/systemctl", state, "--quiet", unit],
            check=False,
        ).returncode
        == 0
    )


def _services() -> None:
    monolithic = ("libvirtd.socket", "libvirtd.service")
    if any(_unit_has_state("is-active", unit) for unit in monolithic):
        return
    for unit in monolithic:
        if _unit_has_state("is-enabled", unit):
            _run("/usr/bin/systemctl", "start", unit)
            return
    _run(
        "/usr/bin/systemctl",
        "enable",
        "--now",
        "virtqemud.socket",
        "virtqemud-ro.socket",
        "virtstoraged.socket",
        "virtstoraged-ro.socket",
    )


def _virsh(*args: str) -> str:
    return _run("/usr/bin/virsh", "--connect", URI, *args)


def _pool(target: Path) -> None:
    if target.resolve() != target:
        raise SetupError(f"Use a real pool directory at {target}")

    names = _virsh("pool-list", "--all", "--name").splitlines()
    existing = POOL in names
    if existing:
        if (
            POOL
            not in _virsh("pool-list", "--all", "--persistent", "--name").splitlines()
        ):
            raise SetupError(
                f"Storage pool {POOL!r} is temporary. Select it manually if appropriate"
            )
        _check_pool_definition(POOL, target)
    else:
        for name in filter(None, names):
            if _pool_target(name) == target:
                raise SetupError(f"{target} is already used by storage pool {name!r}")

    created_target = not target.exists()
    if not created_target:
        entry = target.stat()
        if entry.st_uid != 0 or entry.st_mode & 0o022:
            raise SetupError(
                f"{target} must be owned by root and protected from other writers"
            )
        if not stat.S_ISDIR(entry.st_mode):
            raise SetupError(f"Use a directory at {target}")
        if not existing and any(target.iterdir()):
            raise SetupError(
                f"{target} already contains files. Use a different pool or review it manually"
            )
    else:
        target.mkdir(mode=0o711)
    if not existing or created_target:
        _run("/usr/bin/restorecon", os.fspath(target))

    if not existing:
        _virsh("pool-define-as", POOL, "dir", "--target", os.fspath(target))
    if POOL not in _virsh("pool-list", "--name").splitlines():
        _virsh("pool-start", POOL)
    _virsh("pool-autostart", POOL)


def _pool_xml(name: str, *flags: str) -> ElementTree.Element:
    xml = _virsh("pool-dumpxml", "--pool", name, *flags)
    return ElementTree.fromstring(xml)  # noqa: S314 - Local libvirt daemon response.


def _pool_target(name: str) -> Path | None:
    path = _pool_xml(name).findtext("target/path")
    return Path(path).resolve() if path else None


def _check_pool_definition(name: str, target: Path) -> None:
    for flags in ((), ("--inactive",)):
        root = _pool_xml(name, *flags)
        if root.get("type") != "dir" or root.findtext("target/path") != str(target):
            raise SetupError(
                f"Storage pool {name!r} already exists with another type or path. Select it manually if appropriate"
            )


def main(arguments: Sequence[str] = ()) -> int:
    if os.geteuid() != 0 or arguments:
        print("Run ./setup-fedora.sh to prepare the Fedora host.", file=sys.stderr)
        return 2
    try:
        _services()
        _pool(TARGET)
    except (
        OSError,
        subprocess.CalledProcessError,
        ElementTree.ParseError,
        SetupError,
    ) as error:
        print(f"FocusVM host setup stopped. {error}", file=sys.stderr)
        return 1
    print("Fedora host tools and the focusvm storage pool are ready.")
    print(
        "Run ./install.sh as your regular user, then run focusvm setup or open FocusVM."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
