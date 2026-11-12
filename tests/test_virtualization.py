# The fake driver exercises the private native binding boundary and XML helpers.
# pyright: reportPrivateUsage=false

from dataclasses import replace
from pathlib import Path
from typing import cast
from uuid import UUID
from xml.etree import ElementTree as ET

import pytest

from focusvm.virtualization import (
    MANAGED_TAG,
    DomainNotFound,
    LibvirtBackend,
    VirtualizationError,
    VMDefinition,
    Volume,
    _Connection,
    _domain_xml,
    _DriverError,
    _Libvirt,
    _serialize,
    _xml,
)

VM_ID = UUID(int=1)
OWNER = UUID(int=2)
FOREIGN_OWNER = UUID(int=3)
OVERLAY = Volume("overlay.qcow2", "overlay-key", Path("/storage/overlay.qcow2"))
SEED = Volume("seed.iso", "seed-key", Path("/storage/seed.iso"))
DEFINITION = VMDefinition(VM_ID, "focusvm-test", OWNER, 2, 4096, OVERLAY, SEED)


class DriverError(_DriverError):
    def get_error_code(self) -> int:
        return 42


class Driver:
    libvirtError = DriverError
    VIR_ERR_NO_DOMAIN = 42
    VIR_DOMAIN_XML_INACTIVE = 2
    VIR_DOMAIN_DEFINE_VALIDATE = 1
    VIR_DOMAIN_START_PAUSED = 1
    VIR_DOMAIN_PAUSED = 3
    VIR_DOMAIN_SHUTDOWN_ACPI_POWER_BTN = 1
    VIR_STORAGE_VOL_GET_PHYSICAL = 1


class StorageVolume:
    def __init__(self, volume: Volume) -> None:
        self.volume = volume

    def path(self) -> str:
        return str(self.volume.path)

    def infoFlags(self, flags: int) -> list[int]:
        assert flags == Driver.VIR_STORAGE_VOL_GET_PHYSICAL
        return [0, 32 * 1024**3, 5 * 1024**2]


class Domain:
    def __init__(self, definition: VMDefinition = DEFINITION) -> None:
        self.inactive_xml = _domain_xml(definition)
        self.live_xml: ET.Element | None = None
        self.state = 5
        self.saved = False
        self.events: list[str] = []
        self.accept_shutdown = True

    def name(self) -> str:
        return self.inactive_xml.findtext("name", "")

    def UUIDString(self) -> str:
        return self.inactive_xml.findtext("uuid", "")

    def isActive(self) -> int:
        return int(self.state != 5)

    def isPersistent(self) -> int:
        return 1

    def info(self) -> list[int]:
        memory = int(self.inactive_xml.findtext("memory", "0"))
        cpus = int(self.inactive_xml.findtext("vcpu", "0"))
        return [self.state, memory, memory, cpus, 0]

    def XMLDesc(self, flags: int) -> str:
        if flags == 0 and self.live_xml is not None:
            return _serialize(self.live_xml)
        return _serialize(self.inactive_xml)

    def hasManagedSaveImage(self, flags: int) -> int:
        assert flags == 0
        return int(self.saved)

    def createWithFlags(self, flags: int) -> int:
        assert flags == Driver.VIR_DOMAIN_START_PAUSED
        self.state = 3
        self.events.append("start-paused")
        return 0

    def resume(self) -> int:
        assert self.state == 3
        self.state = 1
        self.events.append("resume")
        return 0

    def destroy(self) -> int:
        assert self.state == 3
        self.state = 5
        self.events.append("discard-paused-boot")
        return 0

    def shutdownFlags(self, flags: int) -> int:
        assert flags == Driver.VIR_DOMAIN_SHUTDOWN_ACPI_POWER_BTN
        self.events.append("graceful-stop")
        if self.accept_shutdown:
            self.state = 5
        return 0


class Connection:
    def __init__(self, domain: Domain | None) -> None:
        self.domain = domain
        self.define_calls = 0

    def lookupByUUIDString(self, uuid: str) -> Domain:
        if self.domain is not None and self.domain.UUIDString() == uuid:
            return self.domain
        raise DriverError()

    def lookupByName(self, name: str) -> Domain:
        if self.domain is not None and self.domain.name() == name:
            return self.domain
        raise DriverError()

    def storageVolLookupByKey(self, key: str) -> StorageVolume:
        return StorageVolume({OVERLAY.key: OVERLAY, SEED.key: SEED}[key])

    def defineXMLFlags(self, xml: str, flags: int) -> Domain:
        assert flags == Driver.VIR_DOMAIN_DEFINE_VALIDATE
        self.define_calls += 1
        domain = self.domain or Domain()
        domain.inactive_xml = _xml(xml)
        self.domain = domain
        return domain

    def backend(self) -> LibvirtBackend:
        return LibvirtBackend(cast("_Libvirt", Driver()), cast("_Connection", self))


def test_define_creates_offline_domain_and_reports_resources() -> None:
    connection = Connection(None)
    backend = connection.backend()

    vm = backend.define(DEFINITION)

    assert vm.uuid == VM_ID
    assert vm.state == "shut-off"
    assert vm.memory_mib == 4096
    assert vm.disk_capacity_bytes == 32 * 1024**3
    assert vm.disk_allocation_bytes == 5 * 1024**2
    assert connection.domain is not None
    root = connection.domain.inactive_xml
    assert root.find("devices/interface") is None
    assert root.find("devices/graphics/listen[@type='none']") is not None
    assert root.find("devices/disk[@device='cdrom']/readonly") is not None
    assert backend.define(DEFINITION) == vm
    assert connection.define_calls == 1


def test_define_does_not_replace_foreign_name_or_uuid() -> None:
    connection = Connection(Domain(replace(DEFINITION, owner_id=FOREIGN_OWNER)))
    backend = connection.backend()

    with pytest.raises(VirtualizationError, match="not owned"):
        backend.define(DEFINITION)
    with pytest.raises(VirtualizationError, match="name.*already in use"):
        backend.define(replace(DEFINITION, uuid=UUID(int=4)))
    with pytest.raises(DomainNotFound):
        backend.status(UUID(int=4), OWNER)

    assert connection.define_calls == 0


def test_inventory_rejects_a_changed_live_volume_key() -> None:
    domain = Domain()
    domain.state = 1
    domain.live_xml = _domain_xml(DEFINITION)
    metadata = domain.live_xml.find(f"metadata/{MANAGED_TAG}")
    assert metadata is not None
    metadata.set("overlay", "other-disk")
    backend = Connection(domain).backend()

    with pytest.raises(VirtualizationError, match="recorded volume identities"):
        backend.check_inventory(VM_ID, OWNER, OVERLAY.key, SEED.key)


def test_start_validates_while_paused_then_resumes() -> None:
    domain = Domain()
    backend = Connection(domain).backend()

    assert backend.start(VM_ID, OWNER).state == "running"
    assert backend.start(VM_ID, OWNER).active
    assert domain.events == ["start-paused", "resume"]


def test_start_does_not_resume_guest_with_unexpected_live_device() -> None:
    domain = Domain()
    domain.live_xml = _domain_xml(DEFINITION)
    devices = domain.live_xml.find("devices")
    assert devices is not None
    ET.SubElement(devices, "interface", {"type": "user"})
    backend = Connection(domain).backend()

    with pytest.raises(VirtualizationError, match="unexpected device"):
        backend.start(VM_ID, OWNER)
    assert domain.state == 5
    assert domain.events == ["start-paused", "discard-paused-boot"]


def test_start_can_resume_a_valid_paused_domain_after_interruption() -> None:
    domain = Domain()
    domain.state = 3
    backend = Connection(domain).backend()

    assert backend.start(VM_ID, OWNER).state == "running"
    assert domain.events == ["resume"]


def test_managed_save_prevents_start() -> None:
    domain = Domain()
    domain.saved = True
    backend = Connection(domain).backend()

    with pytest.raises(VirtualizationError, match="managed-save"):
        backend.start(VM_ID, OWNER)
    assert domain.events == []


def test_shutdown_timeout_preserves_running_vm() -> None:
    domain = Domain()
    domain.state = 1
    domain.accept_shutdown = False
    backend = Connection(domain).backend()

    with pytest.raises(VirtualizationError, match="still running"):
        backend.stop(VM_ID, OWNER, timeout=0.01)
    assert backend.status(VM_ID, OWNER).active
    assert domain.events == ["graceful-stop"]


def test_stop_and_configure_use_owned_inactive_domain() -> None:
    domain = Domain()
    domain.state = 1
    backend = Connection(domain).backend()

    with pytest.raises(VirtualizationError, match="Shut down"):
        backend.configure(VM_ID, OWNER, 4, 8192)
    assert backend.stop(VM_ID, OWNER, timeout=1).state == "shut-off"
    assert backend.stop(VM_ID, OWNER, timeout=1).state == "shut-off"
    updated = backend.configure(VM_ID, OWNER, 4, 8192)
    assert (updated.cpus, updated.memory_mib) == (4, 8192)
    assert domain.events == ["graceful-stop"]
