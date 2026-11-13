"""Manage local QEMU/KVM domains and storage through libvirt."""

from __future__ import annotations

import hashlib
import importlib
import math
import os
import time
from abc import ABC, abstractmethod
from collections.abc import Generator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast
from uuid import UUID, uuid4
from xml.etree import ElementTree as ET

URI = "qemu:///system"
ARCH = "x86_64"
MACHINE = "q35"
METADATA_NS = "https://focusvm.local/libvirt/1"
MANAGED_TAG = f"{{{METADATA_NS}}}managed"
CHUNK_SIZE = 1024 * 1024

DOMAIN_STATES = {
    0: "no-state",
    1: "running",
    2: "blocked",
    3: "paused",
    4: "shutting-down",
    5: "shut-off",
    6: "crashed",
    7: "suspended",
}


class VirtualizationError(RuntimeError):
    """An operation could not be completed through libvirt."""


class VirtualizationUnavailable(VirtualizationError):
    """The system connection or required virtualization capability is unavailable."""


class DomainNotFound(VirtualizationError):
    """Libvirt has no domain with the requested UUID."""


@dataclass(frozen=True, slots=True)
class Volume:
    name: str
    key: str
    path: Path


@dataclass(frozen=True, slots=True)
class PoolStatus:
    name: str
    available_bytes: int


@dataclass(frozen=True, slots=True)
class VMDefinition:
    uuid: UUID
    name: str
    owner_id: UUID
    cpus: int
    memory_mib: int
    overlay: Volume
    seed: Volume


@dataclass(frozen=True, slots=True)
class VMStatus:
    uuid: UUID
    name: str
    state: str
    active: bool
    cpus: int
    memory_mib: int
    disk_capacity_bytes: int
    disk_allocation_bytes: int


class _DriverError(Exception, ABC):
    @abstractmethod
    def get_error_code(self) -> int: ...


class _Stream(Protocol):
    def send(self, data: bytes) -> int: ...
    def recv(self, nbytes: int) -> bytes: ...
    def finish(self) -> int: ...
    def abort(self) -> int: ...


class _Volume(Protocol):
    def name(self) -> str: ...
    def key(self) -> str: ...
    def path(self) -> str: ...
    def XMLDesc(self, flags: int) -> str: ...
    def infoFlags(self, flags: int) -> list[int]: ...
    def upload(self, stream: _Stream, offset: int, length: int, flags: int) -> int: ...
    def download(
        self, stream: _Stream, offset: int, length: int, flags: int
    ) -> int: ...


class _Pool(Protocol):
    def isActive(self) -> int: ...
    def XMLDesc(self, flags: int) -> str: ...
    def info(self) -> list[int]: ...
    def listVolumes(self) -> list[str]: ...
    def refresh(self, flags: int) -> int: ...
    def storageVolLookupByName(self, name: str) -> _Volume: ...
    def createXML(self, xml: str, flags: int) -> _Volume: ...


class _Domain(Protocol):
    def name(self) -> str: ...
    def UUIDString(self) -> str: ...
    def isActive(self) -> int: ...
    def isPersistent(self) -> int: ...
    def info(self) -> list[int]: ...
    def XMLDesc(self, flags: int) -> str: ...
    def hasManagedSaveImage(self, flags: int) -> int: ...
    def createWithFlags(self, flags: int) -> int: ...
    def resume(self) -> int: ...
    def destroy(self) -> int: ...
    def shutdownFlags(self, flags: int) -> int: ...


class _Connection(Protocol):
    def getDomainCapabilities(
        self,
        emulatorbin: str | None,
        arch: str,
        machine: str,
        virttype: str,
        flags: int,
    ) -> str: ...
    def storagePoolLookupByName(self, name: str) -> _Pool: ...
    def storageVolLookupByKey(self, key: str) -> _Volume: ...
    def newStream(self, flags: int) -> _Stream: ...
    def lookupByUUIDString(self, uuid: str) -> _Domain: ...
    def lookupByName(self, name: str) -> _Domain: ...
    def defineXMLFlags(self, xml: str, flags: int) -> _Domain: ...
    def close(self) -> int: ...


class _Libvirt(Protocol):
    libvirtError: type[_DriverError]
    VIR_ERR_NO_DOMAIN: int
    VIR_DOMAIN_XML_INACTIVE: int
    VIR_DOMAIN_DEFINE_VALIDATE: int
    VIR_DOMAIN_START_PAUSED: int
    VIR_DOMAIN_PAUSED: int
    VIR_DOMAIN_SHUTDOWN_ACPI_POWER_BTN: int
    VIR_STORAGE_VOL_GET_PHYSICAL: int

    def open(self, uri: str) -> _Connection | None: ...
    def openReadOnly(self, uri: str) -> _Connection | None: ...


@contextmanager
def connect(*, read_only: bool = False) -> Generator[LibvirtBackend]:
    try:
        driver = cast("_Libvirt", importlib.import_module("libvirt"))
    except (ImportError, OSError) as error:
        raise VirtualizationUnavailable(
            "Install the libvirt Python bindings for the interpreter running FocusVM"
        ) from error

    try:
        connection = driver.openReadOnly(URI) if read_only else driver.open(URI)
    except driver.libvirtError as error:
        raise VirtualizationUnavailable(f"Cannot connect to {URI}: {error}") from error
    if connection is None:
        raise VirtualizationUnavailable(f"Cannot connect to {URI}")
    try:
        yield LibvirtBackend(driver, connection)
    finally:
        connection.close()


class LibvirtBackend:
    def __init__(self, driver: _Libvirt, connection: _Connection) -> None:
        self._driver = driver
        self._connection = connection

    def check_capabilities(self) -> None:
        try:
            capabilities = _xml(
                self._connection.getDomainCapabilities(None, ARCH, MACHINE, "kvm", 0)
            )
        except self._driver.libvirtError as error:
            raise VirtualizationUnavailable(
                f"Libvirt cannot provide x86_64 KVM on q35: {error}"
            ) from error
        if (
            capabilities.findtext("domain") != "kvm"
            or capabilities.findtext("arch") != ARCH
        ):
            raise VirtualizationUnavailable(
                "Libvirt did not report x86_64 KVM capabilities"
            )
        graphics = capabilities.find("devices/graphics[@supported='yes']")
        if graphics is None or "spice" not in _enum_values(graphics, "type"):
            raise VirtualizationUnavailable(
                "QEMU does not advertise SPICE display support"
            )

    def pool_status(self, pool: str) -> PoolStatus:
        with self._operation(f"Cannot inspect storage pool {pool!r}"):
            _, _, _, available = self._directory_pool(pool).info()
            return PoolStatus(pool, available)

    def import_base(
        self, pool: str, path: Path, sha256: str, size_bytes: int, owner_id: UUID
    ) -> Volume:
        if len(sha256) != 64 or any(char not in "0123456789abcdef" for char in sha256):
            raise VirtualizationError(
                "The guest image SHA-256 must be lowercase hexadecimal"
            )
        prefix = f"focusvm-base-{owner_id.hex}-{sha256}"
        file_size = path.stat().st_size
        with self._operation(f"Cannot import guest image into {pool!r}"):
            storage = self._directory_pool(pool)
            storage.refresh(0)
            names = storage.listVolumes()
            for name in names:
                if name == f"{prefix}.qcow2" or (
                    name.startswith(f"{prefix}-") and name.endswith(".qcow2")
                ):
                    candidate = storage.storageVolLookupByName(name)
                    if self._base_matches(candidate, file_size, size_bytes, sha256):
                        return _volume(candidate)

            name = f"{prefix}.qcow2"
            if name in names:
                name = f"{prefix}-{uuid4().hex}.qcow2"
            base = self._upload(storage, name, path)
            if not self._base_matches(base, file_size, size_bytes, sha256):
                raise VirtualizationError(
                    f"Imported volume {name!r} failed verification"
                )
            return _volume(base)

    def create_overlay(
        self, pool: str, name: str, base: Volume, capacity_bytes: int
    ) -> Volume:
        with self._operation(f"Cannot create overlay {name!r}"):
            storage = self._directory_pool(pool)
            volume_xml = _volume_xml(name, capacity_bytes, "qcow2")
            backing = ET.SubElement(volume_xml, "backingStore")
            ET.SubElement(backing, "path").text = os.fspath(base.path)
            ET.SubElement(backing, "format", {"type": "qcow2"})
            created = storage.createXML(_serialize(volume_xml), 0)
            description = _xml(created.XMLDesc(0))
            if description.findtext("backingStore/path") != os.fspath(base.path):
                raise VirtualizationError(
                    f"Overlay {name!r} has an unexpected backing image"
                )
            return _volume(created)

    def import_seed(self, pool: str, name: str, path: Path) -> Volume:
        with self._operation(f"Cannot import first-boot media {name!r}"):
            return _volume(self._upload(self._directory_pool(pool), name, path))

    def define(self, definition: VMDefinition) -> VMStatus:
        _validate_resources(definition.cpus, definition.memory_mib)
        with self._operation(f"Cannot define VM {definition.name!r}"):
            existing = self._find_domain(definition.uuid)
            if existing is not None:
                root = self._owned_xml(existing, definition.owner_id)
                metadata = _metadata(root)
                if (
                    existing.name() != definition.name
                    or metadata is None
                    or metadata.get("overlay") != definition.overlay.key
                    or metadata.get("seed") != definition.seed.key
                ):
                    raise VirtualizationError(
                        f"VM UUID {definition.uuid} is already in use"
                    )
                self._validate_offline(root)
                return self._status(existing, root)
            try:
                self._connection.lookupByName(definition.name)
            except self._driver.libvirtError as error:
                if error.get_error_code() != self._driver.VIR_ERR_NO_DOMAIN:
                    raise
            else:
                raise VirtualizationError(
                    f"VM name {definition.name!r} is already in use"
                )

            domain = self._connection.defineXMLFlags(
                _serialize(_domain_xml(definition)),
                self._driver.VIR_DOMAIN_DEFINE_VALIDATE,
            )
            root = self._owned_xml(domain, definition.owner_id)
            self._validate_offline(root)
            return self._status(domain, root)

    def status(self, uuid: UUID, owner_id: UUID) -> VMStatus:
        with self._operation(f"Cannot inspect VM {uuid}"):
            domain = self._require_domain(uuid)
            return self._status(domain, self._owned_xml(domain, owner_id))

    def check_console(self, uuid: UUID, owner_id: UUID) -> None:
        with self._operation(f"Cannot open the console for VM {uuid}"):
            domain = self._require_domain(uuid)
            if not domain.isActive():
                raise VirtualizationError("Start the VM before opening its console")
            self._validate_offline(self._owned_xml(domain, owner_id))
            self._validate_offline(self._owned_xml(domain, owner_id, live=True))

    def check_inventory(
        self, uuid: UUID, owner_id: UUID, overlay_key: str, seed_key: str
    ) -> None:
        with self._operation(f"Cannot check VM {uuid} against the inventory"):
            domain = self._require_domain(uuid)
            roots = [self._owned_xml(domain, owner_id)]
            if domain.isActive():
                roots.append(self._owned_xml(domain, owner_id, live=True))
            for root in roots:
                metadata = _metadata(root)
                if (
                    metadata is None
                    or metadata.get("overlay") != overlay_key
                    or metadata.get("seed") != seed_key
                ):
                    raise VirtualizationError(
                        f"VM {uuid} no longer matches its recorded volume identities"
                    )
                self._validate_volume_paths(root)

    def start(self, uuid: UUID, owner_id: UUID) -> VMStatus:
        with self._operation(f"Cannot start VM {uuid}"):
            domain = self._require_domain(uuid)
            root = self._owned_xml(domain, owner_id)
            self._validate_offline(root)
            if domain.isActive():
                self._validate_offline(self._owned_xml(domain, owner_id, live=True))
                if domain.info()[0] == self._driver.VIR_DOMAIN_PAUSED:
                    domain.resume()
                return self._status(domain, root)
            if domain.hasManagedSaveImage(0):
                raise VirtualizationError(
                    "The VM has a managed-save image. Resolve it before starting the VM."
                )

            domain.createWithFlags(self._driver.VIR_DOMAIN_START_PAUSED)
            try:
                self._validate_offline(self._owned_xml(domain, owner_id, live=True))
                domain.resume()
            except (VirtualizationError, self._driver.libvirtError) as error:
                with suppress(self._driver.libvirtError):
                    if domain.info()[0] == self._driver.VIR_DOMAIN_PAUSED:
                        domain.destroy()
                raise VirtualizationError(
                    f"VM {uuid} failed to start with a verified live configuration: {error}. "
                    "Check its current state before retrying."
                ) from error
            return self._status(domain, root)

    def stop(self, uuid: UUID, owner_id: UUID, timeout: float) -> VMStatus:
        if not math.isfinite(timeout) or timeout <= 0:
            raise VirtualizationError("Shutdown timeout must be positive and finite")
        with self._operation(f"Cannot stop VM {uuid}"):
            domain = self._require_domain(uuid)
            root = self._owned_xml(domain, owner_id)
            if domain.isActive():
                self._owned_xml(domain, owner_id, live=True)
                domain.shutdownFlags(self._driver.VIR_DOMAIN_SHUTDOWN_ACPI_POWER_BTN)
                deadline = time.monotonic() + timeout
                while domain.isActive():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise VirtualizationError(
                            f"VM {uuid} is still running after {timeout:g} seconds"
                        )
                    time.sleep(min(0.25, remaining))
            return self._status(domain, root)

    def configure(
        self, uuid: UUID, owner_id: UUID, cpus: int, memory_mib: int
    ) -> VMStatus:
        _validate_resources(cpus, memory_mib)
        with self._operation(f"Cannot configure VM {uuid}"):
            domain = self._require_domain(uuid)
            root = self._owned_xml(domain, owner_id)
            if domain.isActive() or domain.hasManagedSaveImage(0):
                raise VirtualizationError(
                    "Shut down the VM before changing its resources"
                )
            self._validate_offline(root)
            _set_resources(root, cpus, memory_mib)
            domain = self._connection.defineXMLFlags(
                _serialize(root), self._driver.VIR_DOMAIN_DEFINE_VALIDATE
            )
            return self._status(domain, self._owned_xml(domain, owner_id))

    @contextmanager
    def _operation(self, operation: str) -> Generator[None]:
        try:
            yield
        except self._driver.libvirtError as error:
            raise VirtualizationError(f"{operation}: {error}") from error

    def _directory_pool(self, name: str) -> _Pool:
        pool = self._connection.storagePoolLookupByName(name)
        if not pool.isActive():
            raise VirtualizationError(f"Storage pool {name!r} is inactive")
        if _xml(pool.XMLDesc(0)).get("type") != "dir":
            raise VirtualizationError(f"Storage pool {name!r} must be directory-backed")
        return pool

    def _upload(self, pool: _Pool, name: str, path: Path) -> _Volume:
        # Upload bytes into a raw file first. A refresh identifies the actual image format.
        with path.open("rb") as source:
            length = os.fstat(source.fileno()).st_size
            volume = pool.createXML(_serialize(_volume_xml(name, length, "raw")), 0)
            stream = self._connection.newStream(0)
            try:
                volume.upload(stream, 0, length, 0)
                while chunk := source.read(CHUNK_SIZE):
                    while chunk:
                        written = stream.send(chunk)
                        if written <= 0:
                            raise VirtualizationError(
                                f"Upload to volume {name!r} stalled"
                            )
                        chunk = chunk[written:]
                stream.finish()
            except (OSError, VirtualizationError, self._driver.libvirtError):
                with suppress(self._driver.libvirtError):
                    stream.abort()
                raise
        pool.refresh(0)
        return pool.storageVolLookupByName(name)

    def _base_matches(
        self, volume: _Volume, file_size: int, capacity: int, sha256: str
    ) -> bool:
        description = _xml(volume.XMLDesc(0))
        if (
            description.find("target/format[@type='qcow2']") is None
            or description.find("backingStore/path") is not None
            or volume.infoFlags(0)[1] != capacity
        ):
            return False

        digest = hashlib.sha256()
        bytes_read = 0
        stream = self._connection.newStream(0)
        try:
            volume.download(stream, 0, 0, 0)
            while chunk := stream.recv(CHUNK_SIZE):
                bytes_read += len(chunk)
                if bytes_read > file_size:
                    stream.abort()
                    return False
                digest.update(chunk)
            stream.finish()
        except self._driver.libvirtError:
            with suppress(self._driver.libvirtError):
                stream.abort()
            raise
        return bytes_read == file_size and digest.hexdigest() == sha256

    def _find_domain(self, uuid: UUID) -> _Domain | None:
        try:
            return self._connection.lookupByUUIDString(str(uuid))
        except self._driver.libvirtError as error:
            if error.get_error_code() == self._driver.VIR_ERR_NO_DOMAIN:
                return None
            raise

    def _require_domain(self, uuid: UUID) -> _Domain:
        domain = self._find_domain(uuid)
        if domain is None:
            raise DomainNotFound(f"No libvirt domain with UUID {uuid}")
        return domain

    def _owned_xml(
        self, domain: _Domain, owner_id: UUID, *, live: bool = False
    ) -> ET.Element:
        if not domain.isPersistent():
            raise VirtualizationError(f"VM {domain.UUIDString()} is not persistent")
        flags = 0 if live else self._driver.VIR_DOMAIN_XML_INACTIVE
        root = _xml(domain.XMLDesc(flags))
        if not _owner_matches(root, owner_id):
            raise VirtualizationError(
                f"VM {domain.UUIDString()} is not owned by this FocusVM instance"
            )
        return root

    def _status(self, domain: _Domain, root: ET.Element) -> VMStatus:
        overlay, _ = self._storage(root)
        _, capacity, allocated = overlay.infoFlags(
            self._driver.VIR_STORAGE_VOL_GET_PHYSICAL
        )
        state, max_memory, memory, cpus, _ = domain.info()
        return VMStatus(
            uuid=UUID(domain.UUIDString()),
            name=domain.name(),
            state=DOMAIN_STATES.get(state, "unknown"),
            active=bool(domain.isActive()),
            cpus=cpus,
            memory_mib=(memory or max_memory) // 1024,
            disk_capacity_bytes=capacity,
            disk_allocation_bytes=allocated,
        )

    def _storage(self, root: ET.Element) -> tuple[_Volume, _Volume]:
        metadata = _metadata(root)
        if metadata is None:
            raise VirtualizationError("VM is missing its FocusVM metadata")
        overlay_key = metadata.get("overlay")
        seed_key = metadata.get("seed")
        if not overlay_key or not seed_key or overlay_key == seed_key:
            raise VirtualizationError("VM is missing its FocusVM volume identities")
        return (
            self._connection.storageVolLookupByKey(overlay_key),
            self._connection.storageVolLookupByKey(seed_key),
        )

    def _validate_offline(self, root: ET.Element) -> None:
        _validate_offline_devices(root)
        self._validate_volume_paths(root)

    def _validate_volume_paths(self, root: ET.Element) -> None:
        overlay, seed = self._storage(root)
        _validate_disk(root, "vda", "disk", overlay.path(), "qcow2")
        _validate_disk(root, "sda", "cdrom", seed.path(), "raw")


def _xml(document: str) -> ET.Element:
    # Inputs come from the authenticated local libvirt connection.
    try:
        return ET.fromstring(document)  # noqa: S314
    except ET.ParseError as error:
        raise VirtualizationError(f"Libvirt returned invalid XML: {error}") from error


def _serialize(root: ET.Element) -> str:
    return ET.tostring(root, encoding="unicode")


def _enum_values(parent: ET.Element, name: str) -> set[str | None]:
    return {value.text for value in parent.findall(f"enum[@name='{name}']/value")}


def _volume(volume: _Volume) -> Volume:
    return Volume(volume.name(), volume.key(), Path(volume.path()))


def _volume_xml(name: str, capacity: int, format_name: str) -> ET.Element:
    if not name or name in {".", ".."} or "/" in name or not name.isprintable():
        raise VirtualizationError("Invalid libvirt volume name")
    if capacity <= 0:
        raise VirtualizationError("Libvirt volume capacity must be positive")
    root = ET.Element("volume")
    ET.SubElement(root, "name").text = name
    ET.SubElement(root, "capacity", {"unit": "bytes"}).text = str(capacity)
    ET.SubElement(root, "allocation", {"unit": "bytes"}).text = "0"
    target = ET.SubElement(root, "target")
    ET.SubElement(target, "format", {"type": format_name})
    ET.SubElement(ET.SubElement(target, "permissions"), "mode").text = "0600"
    return root


def _metadata(root: ET.Element) -> ET.Element | None:
    return root.find(f"metadata/{MANAGED_TAG}")


def _owner_matches(root: ET.Element, owner_id: UUID) -> bool:
    metadata = _metadata(root)
    return metadata is not None and metadata.get("owner") == str(owner_id)


def _validate_resources(cpus: int, memory_mib: int) -> None:
    if (
        type(cpus) is not int
        or type(memory_mib) is not int
        or cpus <= 0
        or memory_mib <= 0
    ):
        raise VirtualizationError("VM CPU count and memory must be positive integers")


def _set_resources(root: ET.Element, cpus: int, memory_mib: int) -> None:
    for tag in ("memory", "currentMemory", "vcpu"):
        node = root.find(tag)
        if node is None:
            node = ET.SubElement(root, tag)
        if tag == "vcpu":
            node.attrib.pop("current", None)
            node.text = str(cpus)
        else:
            node.set("unit", "KiB")
            node.text = str(memory_mib * 1024)


def _domain_xml(definition: VMDefinition) -> ET.Element:
    root = ET.Element("domain", {"type": "kvm"})
    ET.SubElement(root, "name").text = definition.name
    ET.SubElement(root, "uuid").text = str(definition.uuid)
    ET.SubElement(
        ET.SubElement(root, "metadata"),
        MANAGED_TAG,
        {
            "owner": str(definition.owner_id),
            "overlay": definition.overlay.key,
            "seed": definition.seed.key,
        },
    )
    _set_resources(root, definition.cpus, definition.memory_mib)
    os_node = ET.SubElement(root, "os")
    ET.SubElement(os_node, "type", {"arch": ARCH, "machine": MACHINE}).text = "hvm"
    ET.SubElement(os_node, "boot", {"dev": "hd"})
    features = ET.SubElement(root, "features")
    ET.SubElement(features, "acpi")
    ET.SubElement(features, "apic")
    ET.SubElement(root, "on_poweroff").text = "destroy"
    devices = ET.SubElement(root, "devices")
    _add_disk(devices, definition.overlay, "vda", "disk", "qcow2", "virtio")
    _add_disk(devices, definition.seed, "sda", "cdrom", "raw", "sata")
    graphics = ET.SubElement(devices, "graphics", {"type": "spice"})
    ET.SubElement(graphics, "listen", {"type": "none"})
    ET.SubElement(graphics, "clipboard", {"copypaste": "no"})
    ET.SubElement(graphics, "filetransfer", {"enable": "no"})
    ET.SubElement(ET.SubElement(devices, "video"), "model", {"type": "vga"})
    ET.SubElement(devices, "audio", {"id": "1", "type": "none"})
    return root


def _add_disk(
    devices: ET.Element,
    volume: Volume,
    target: str,
    device: str,
    format_name: str,
    bus: str,
) -> None:
    disk = ET.SubElement(devices, "disk", {"type": "file", "device": device})
    ET.SubElement(disk, "driver", {"name": "qemu", "type": format_name})
    ET.SubElement(disk, "source", {"file": os.fspath(volume.path)})
    ET.SubElement(disk, "target", {"dev": target, "bus": bus})
    if device == "cdrom":
        ET.SubElement(disk, "readonly")


def _validate_offline_devices(root: ET.Element) -> None:
    os_type = root.find("os/type")
    if (
        root.get("type") != "kvm"
        or os_type is None
        or os_type.text != "hvm"
        or os_type.get("arch") != ARCH
        or not _is_q35(os_type.get("machine", ""))
        or root.find("os/loader") is not None
        or root.find("os/kernel") is not None
        or any(child.tag.startswith("{") for child in root)
    ):
        raise VirtualizationError(
            "VM definition does not match the managed KVM guest type"
        )

    devices = root.find("devices")
    allowed = {
        "emulator",
        "disk",
        "controller",
        "input",
        "graphics",
        "video",
        "audio",
        "watchdog",
        "memballoon",
    }
    if devices is None or any(device.tag not in allowed for device in devices):
        raise VirtualizationError("VM definition contains an unexpected device")
    for device in devices.findall("input"):
        if (
            device.get("type") not in {"mouse", "keyboard", "tablet"}
            or device.find("source") is not None
        ):
            raise VirtualizationError(
                "VM definition contains an unexpected input device"
            )
    for audio in devices.findall("audio"):
        if audio.get("type") != "none":
            raise VirtualizationError(
                "VM definition contains an unexpected audio backend"
            )
    for watchdog in devices.findall("watchdog"):
        if watchdog.get("model") != "itco":
            raise VirtualizationError("VM definition contains an unexpected watchdog")
    displays = devices.findall("graphics")
    if len(displays) != 1 or not _private_spice(displays[0]):
        raise VirtualizationError(
            "VM display must be private SPICE with sharing disabled"
        )
    if len(devices.findall("disk")) != 2:
        raise VirtualizationError("VM definition contains unexpected storage devices")


def _is_q35(machine: str) -> bool:
    return machine == MACHINE or machine.startswith("pc-q35-")


def _private_spice(graphics: ET.Element) -> bool:
    listeners = graphics.findall("listen")
    return (
        graphics.get("type") == "spice"
        and graphics.get("listen") is None
        and len(listeners) == 1
        and listeners[0].get("type") == "none"
        and graphics.find("clipboard[@copypaste='no']") is not None
        and graphics.find("filetransfer[@enable='no']") is not None
    )


def _validate_disk(
    root: ET.Element, target: str, device: str, path: str, format_name: str
) -> None:
    disk = next(
        (
            entry
            for entry in root.findall("devices/disk")
            if entry.find(f"target[@dev='{target}']") is not None
        ),
        None,
    )
    if (
        disk is None
        or disk.get("device") != device
        or disk.get("type") != "file"
        or disk.find(f"driver[@type='{format_name}']") is None
        or (device == "cdrom" and disk.find("readonly") is None)
        or not any(source.get("file") == path for source in disk.findall("source"))
        or disk.find(".//dataStore") is not None
        or any(
            layer.get("type", "file") != "file" for layer in disk.iter("backingStore")
        )
    ):
        raise VirtualizationError(f"VM disk {target} does not match its managed volume")
