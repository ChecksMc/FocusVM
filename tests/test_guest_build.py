import sys
from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree

import pytest

from guest import smoke
from guest.build import installer_xml


class _MissingDomain(Exception):
    def get_error_code(self) -> int:
        return 42


class _TransientConnection:
    def __init__(self, log: Path, line: str) -> None:
        self.log = log
        self.line = line
        self.destroyed = False

    def createXML(self, xml: str, flags: int, /) -> "_TransientConnection":
        assert xml == "<domain/>"
        assert flags == 2
        self.log.write_text(self.line + "\n", encoding="utf-8")
        return self

    def isActive(self) -> int:
        raise _MissingDomain

    def destroy(self) -> int:
        self.destroyed = True
        return 0


@pytest.mark.parametrize(("line", "passes"), [("COMPLETE", True), ("booting", False)])
def test_transient_guest_disappearance_requires_completion_marker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, line: str, passes: bool
) -> None:
    log = tmp_path / "guest.log"
    connection = _TransientConnection(log, line)
    driver = SimpleNamespace(libvirtError=_MissingDomain, VIR_ERR_NO_DOMAIN=42)
    monkeypatch.setitem(sys.modules, "libvirt", driver)

    if passes:
        smoke.run_guest(connection, "<domain/>", log, "COMPLETE", "FAILED", 1)
    else:
        with pytest.raises(smoke.SmokeError, match="stopped before reporting success"):
            smoke.run_guest(connection, "<domain/>", log, "COMPLETE", "FAILED", 1)

    assert not connection.destroyed


def test_installer_uses_only_one_writable_disk(tmp_path: Path) -> None:
    iso = tmp_path / "fedora.iso"
    domain = ElementTree.fromstring(installer_xml(iso, tmp_path))  # noqa: S314 - generated XML
    writable = [
        disk.find("source")
        for disk in domain.findall("devices/disk")
        if disk.find("readonly") is None
    ]
    assert len(writable) == 1 and writable[0] is not None
    assert writable[0].get("file") == str(tmp_path / "install.qcow2")
    assert domain.find("devices/interface/backend[@type='passt']") is not None
