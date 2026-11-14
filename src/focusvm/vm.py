"""Managed VM operations and their durable local inventory."""

import json
import re
from collections.abc import Generator, Mapping
from contextlib import contextmanager, nullcontext
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Literal, cast
from uuid import UUID, uuid4

from .config import VMDefaults
from .local_state import atomic_write, file_lock, open_owned_file
from .provisioning import ImageInput, VerifiedImage, build_seed, stage_image
from .virtualization import (
    DomainNotFound,
    LibvirtBackend,
    VirtualizationError,
    VMDefinition,
    VMStatus,
    Volume,
    connect,
)

GIB = 1024**3
MAX_DISK_GIB = (2**63 - 1) // GIB

type CreationPhase = Literal["pending", "ready", "abandoned"]
CREATION_PHASES: Mapping[str, CreationPhase] = {
    "pending": "pending",
    "ready": "ready",
    "abandoned": "abandoned",
}


class VMInputError(ValueError):
    """A VM operation was requested with incompatible input."""


class VMStateError(RuntimeError):
    """A VM operation could not be reconciled with local state."""


@dataclass(frozen=True, slots=True)
class _ResourceChange:
    key: str | None


@dataclass(frozen=True, slots=True)
class VMRecord:
    uuid: UUID
    name: str
    storage_pool: str
    image_sha256: str
    resources: VMDefaults
    phase: CreationPhase = "pending"
    base_key: str | None = None
    overlay_key: str | None = None
    seed_key: str | None = None
    resource_key: str | None = None
    resource_change: _ResourceChange | None = None


@dataclass(slots=True)
class _Inventory:
    owner_id: UUID = field(default_factory=uuid4)
    records: dict[UUID, VMRecord] = field(default_factory=dict[UUID, VMRecord])


class VMService:
    def __init__(self, state_dir: Path, cache_dir: Path) -> None:
        self.path = state_dir / "vms.json"
        self.cache_dir = cache_dir

    def create(
        self,
        name: str,
        source: ImageInput,
        password: str,
        storage_pool: str,
        resources: VMDefaults,
    ) -> VMStatus:
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,62}", name) is None:
            raise VMInputError(
                "Use a VM name of 1–63 letters, digits, dots, underscores, or dashes, starting with a letter or digit."
            )
        if resources.disk_gib > MAX_DISK_GIB:
            raise VMInputError(f"VM disk capacity must be at most {MAX_DISK_GIB} GiB")

        with (
            stage_image(source, staging_dir=self.cache_dir) as image,
            file_lock(self.path),
            connect() as backend,
        ):
            inventory = self._load()
            retry = self._existing(
                backend, inventory, name, storage_pool, resources, image
            )
            if retry is not None:
                return retry
            backend.check_capabilities()
            backend.pool_status(storage_pool)
            capacity = resources.disk_gib * GIB
            if capacity < image.virtual_size_bytes:
                raise VMInputError(
                    "VM disk capacity is smaller than the verified guest image"
                )

            record = VMRecord(uuid4(), name, storage_pool, image.sha256, resources)
            with build_seed(record.uuid, password) as seed_path:
                self._save_record(inventory, record)
                return self._provision(
                    backend, inventory, record, image, seed_path, capacity
                )

    def _existing(
        self,
        backend: LibvirtBackend,
        inventory: _Inventory,
        name: str,
        pool: str,
        resources: VMDefaults,
        image: VerifiedImage,
    ) -> VMStatus | None:
        for record in inventory.records.values():
            if record.name != name or record.phase == "abandoned":
                continue
            if (record.storage_pool, record.resources, record.image_sha256) != (
                pool,
                resources,
                image.sha256,
            ):
                raise VMInputError(
                    f"A VM creation record for {name!r} uses different inputs. Use a different name."
                )
            try:
                status = backend.status(record.uuid, inventory.owner_id)
            except DomainNotFound as error:
                if record.phase == "ready":
                    raise VMStateError(
                        f"VM {record.uuid} is missing from libvirt. Its inventory and storage records were preserved."
                    ) from error
                self._save_record(inventory, replace(record, phase="abandoned"))
                return None
            record = self._check_receipts(backend, inventory.owner_id, record)
            if record.phase == "pending":
                self._save_record(inventory, replace(record, phase="ready"))
            else:
                self._persist_reconciled(inventory, record)
            return status
        return None

    def _provision(
        self,
        backend: LibvirtBackend,
        inventory: _Inventory,
        record: VMRecord,
        image: VerifiedImage,
        seed_path: Path,
        capacity: int,
    ) -> VMStatus:
        stem = f"focusvm-{record.uuid}"
        pool = record.storage_pool
        try:
            base = backend.import_base(
                pool,
                image.path,
                image.sha256,
                image.virtual_size_bytes,
                inventory.owner_id,
            )
            record = replace(record, base_key=base.key)
            self._save_record(inventory, record)
            overlay = backend.create_overlay(pool, f"{stem}.qcow2", base, capacity)
            record = replace(record, overlay_key=overlay.key)
            self._save_record(inventory, record)
            seed = backend.import_seed(pool, f"{stem}-seed.iso", seed_path)
            record = replace(record, seed_key=seed.key)
            self._save_record(inventory, record)
            status = backend.define(
                VMDefinition(
                    uuid=record.uuid,
                    name=record.name,
                    owner_id=inventory.owner_id,
                    cpus=record.resources.cpus,
                    memory_mib=record.resources.memory_mib,
                    overlay=overlay,
                    seed=seed,
                )
            )
            self._save_record(inventory, replace(record, phase="ready"))
            return status
        except (OSError, VirtualizationError) as error:
            raise VMStateError(
                f"Could not finish creating VM {record.uuid}. Its inventory and any volumes were preserved. "
                f"Retry with the same name and inputs. {error}"
            ) from error

    def list_records(self) -> tuple[VMRecord, ...]:
        return tuple(self._load().records.values())

    def status(self, target: UUID | None) -> VMStatus:
        with self._domain(target, mutate=False) as (backend, vm, owner):
            return backend.status(vm, owner)

    def check_console(self, target: UUID) -> None:
        with self._domain(target, mutate=False) as (backend, vm, owner):
            backend.check_console(vm, owner)

    def focus_status(self, target: UUID | None) -> VMStatus:
        with self._domain(target, mutate=False) as (backend, vm, owner):
            return backend.focus_status(vm, owner)

    def start(self, target: UUID | None) -> VMStatus:
        with self._domain(target, mutate=True) as (backend, vm, owner):
            return backend.start(vm, owner)

    def stop(self, target: UUID | None, timeout: int) -> VMStatus:
        if not 1 <= timeout <= 3600:
            raise VMInputError("Shutdown timeout must be between 1 and 3600 seconds")
        with self._domain(target, mutate=True) as (backend, vm, owner):
            return backend.stop(vm, owner, timeout)

    def configure(self, target: UUID | None, cpus: int, memory_mib: int) -> VMStatus:
        resources = VMDefaults(cpus=cpus, memory_mib=memory_mib)
        with self._domain(target, mutate=True) as (backend, vm, owner):
            return backend.configure(vm, owner, resources.cpus, resources.memory_mib)

    def attach_resources(self, target: UUID | None, iso: Path) -> VMStatus:
        return self._change_resources(target, iso)

    def eject_resources(self, target: UUID | None) -> VMStatus:
        return self._change_resources(target, None)

    @contextmanager
    def offline_disk(self, target: UUID | None) -> Generator[Path]:
        # Hold the same lock as start and attach throughout the caller's read.
        with file_lock(self.path), connect(read_only=True) as backend:
            inventory = self._load()
            record = self._check_receipts(
                backend, inventory.owner_id, _select_vm(inventory, target)
            )
            if record.base_key is None:
                raise VMStateError("The VM inventory has no backing image receipt")
            yield backend.offline_disk(record.uuid, inventory.owner_id, record.base_key)

    def _change_resources(self, target: UUID | None, iso: Path | None) -> VMStatus:
        with file_lock(self.path), connect() as backend:
            inventory = self._load()
            record = self._check_receipts(
                backend, inventory.owner_id, _select_vm(inventory, target)
            )
            self._persist_reconciled(inventory, record)
            backend.ensure_resources_editable(record.uuid, inventory.owner_id)
            volume: Volume | None = None
            if iso is not None:
                volume = backend.import_resource_iso(
                    record.storage_pool, record.uuid, iso
                )
            desired = None if volume is None else volume.key
            if desired == record.resource_key:
                return backend.status(record.uuid, inventory.owner_id)

            self._save_record(
                inventory, replace(record, resource_change=_ResourceChange(desired))
            )
            status = backend.set_resource_media(record.uuid, inventory.owner_id, volume)
            self._save_record(inventory, replace(record, resource_key=desired))
            return status

    @contextmanager
    def _domain(
        self, target: UUID | None, *, mutate: bool
    ) -> Generator[tuple[LibvirtBackend, UUID, UUID]]:
        with file_lock(self.path) if mutate else nullcontext():
            inventory = self._load()
            record = _select_vm(inventory, target)
            with connect(read_only=not mutate) as backend:
                record = self._check_receipts(backend, inventory.owner_id, record)
                if mutate:
                    self._persist_reconciled(inventory, record)
                yield backend, record.uuid, inventory.owner_id

    def _check_receipts(
        self, backend: LibvirtBackend, owner_id: UUID, record: VMRecord
    ) -> VMRecord:
        if record.overlay_key is None or record.seed_key is None:
            raise VMStateError(
                f"VM {record.uuid} has an incomplete creation record. Retry the create command."
            )
        key = record.resource_key
        if record.resource_change is not None:
            key = backend.resource_media(record.uuid, owner_id)
            if key not in (record.resource_key, record.resource_change.key):
                raise VMStateError(
                    "The VM has an unexpected resource volume after an interrupted change"
                )
        backend.check_inventory(
            record.uuid, owner_id, record.overlay_key, record.seed_key, key
        )
        return replace(record, resource_key=key, resource_change=None)

    def _persist_reconciled(self, inventory: _Inventory, record: VMRecord) -> None:
        if record != inventory.records[record.uuid]:
            self._save_record(inventory, record)

    def _load(self) -> _Inventory:
        try:
            with open(self.path, encoding="utf-8", opener=open_owned_file) as stream:
                payload: object = json.load(stream)
        except FileNotFoundError:
            return _Inventory()
        except ValueError as error:
            raise VMStateError(
                f"Could not read VM inventory {self.path}: {error}"
            ) from error

        try:
            data = _mapping(payload)
            version = data.get("version")
            entries = data.get("records")
            if (
                type(version) is not int
                or version != 1
                or not isinstance(entries, list)
            ):
                raise ValueError("Unsupported inventory format")
            inventory = _Inventory(owner_id=UUID(_required_string(data, "owner_id")))
            for entry in cast("list[object]", entries):
                record = _record(entry)
                if record.uuid in inventory.records:
                    raise ValueError(f"Duplicate VM UUID {record.uuid}")
                inventory.records[record.uuid] = record
            return inventory
        except ValueError as error:
            raise VMStateError(f"Invalid VM inventory {self.path}: {error}") from error

    def _save_record(self, inventory: _Inventory, record: VMRecord) -> None:
        inventory.records[record.uuid] = record
        payload = {
            "version": 1,
            "owner_id": str(inventory.owner_id),
            "records": [asdict(item) for item in inventory.records.values()],
        }
        atomic_write(self.path, json.dumps(payload, indent=2, default=str) + "\n")


def _select_vm(inventory: _Inventory, target: UUID | None) -> VMRecord:
    candidates = [
        record for record in inventory.records.values() if record.phase != "abandoned"
    ]
    if target is not None:
        record = inventory.records.get(target)
        if record is not None and record.phase != "abandoned":
            return record
        raise VMInputError(f"VM {target} is absent from your FocusVM inventory")
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise VMInputError("Create a managed VM first with focusvm create")
    raise VMInputError(
        "More than one VM is recorded. Use focusvm list, then select one with --vm UUID."
    )


def _record(value: object) -> VMRecord:
    data = _mapping(value)
    resources = _mapping(data.get("resources"))
    cpus, memory, disk = (
        resources.get(key) for key in ("cpus", "memory_mib", "disk_gib")
    )
    if type(cpus) is not int or type(memory) is not int or type(disk) is not int:
        raise ValueError("Invalid VM resources")
    phase_value = _required_string(data, "phase")
    phase = CREATION_PHASES.get(phase_value)
    if phase is None:
        raise ValueError(f"Invalid creation phase {phase_value!r}")
    return VMRecord(
        uuid=UUID(_required_string(data, "uuid")),
        name=_required_string(data, "name"),
        storage_pool=_required_string(data, "storage_pool"),
        image_sha256=_required_string(data, "image_sha256"),
        resources=VMDefaults(cpus=cpus, memory_mib=memory, disk_gib=disk),
        phase=phase,
        base_key=_optional_string(data, "base_key"),
        overlay_key=_optional_string(data, "overlay_key"),
        seed_key=_optional_string(data, "seed_key"),
        resource_key=_optional_string(data, "resource_key"),
        resource_change=_resource_change(data.get("resource_change")),
    )


def _resource_change(value: object) -> _ResourceChange | None:
    if value is None:
        return None
    change = _mapping(value)
    if set(change) != {"key"}:
        raise ValueError("Invalid pending resource change")
    return _ResourceChange(_optional_string(change, "key"))


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise ValueError("Expected an inventory object")
    return cast("dict[str, object]", value)


def _required_string(data: Mapping[str, object], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Expected a nonempty inventory field {key!r}")
    return value


def _optional_string(data: Mapping[str, object], key: str) -> str | None:
    return None if data.get(key) is None else _required_string(data, key)
