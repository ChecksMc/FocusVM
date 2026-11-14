import json
from collections.abc import Generator
from contextlib import contextmanager, nullcontext
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest

from focusvm.config import VMDefaults
from focusvm.provisioning import ImageInput, VerifiedImage
from focusvm.virtualization import (
    DomainNotFound,
    PoolStatus,
    VirtualizationError,
    VMDefinition,
    VMStatus,
    Volume,
)
from focusvm.vm import GIB, VMInputError, VMService, VMStateError


class _Backend:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.domains: dict[UUID, tuple[VMDefinition, VMStatus]] = {}
        self.overlays: list[str] = []
        self.fail_seed = False
        self.fail_after_define = False
        self.start_calls = 0
        self.read_modes: list[bool] = []
        self.resource_volumes: dict[UUID, str | None] = {}
        self.fail_after_resource_change = False

    @contextmanager
    def connect(self, *, read_only: bool = False) -> Generator["_Backend"]:
        self.read_modes.append(read_only)
        yield self

    def _volume(self, name: str) -> Volume:
        path = self.root / name
        return Volume(name, str(path), path)

    def check_capabilities(self) -> None:
        pass

    def pool_status(self, name: str) -> PoolStatus:
        return PoolStatus(name, 100 * GIB)

    def import_base(
        self, pool: str, path: Path, sha256: str, size_bytes: int, owner_id: UUID
    ) -> Volume:
        return self._volume(f"base-{owner_id}-{sha256}.qcow2")

    def create_overlay(
        self, pool: str, name: str, base: Volume, capacity_bytes: int
    ) -> Volume:
        assert name not in self.overlays
        self.overlays.append(name)
        return self._volume(name)

    def import_seed(self, pool: str, name: str, path: Path) -> Volume:
        if self.fail_seed:
            raise VirtualizationError("seed upload interrupted")
        return self._volume(name)

    def define(self, definition: VMDefinition) -> VMStatus:
        status = VMStatus(
            definition.uuid,
            definition.name,
            "shut-off",
            False,
            definition.cpus,
            definition.memory_mib,
            32 * GIB,
            512 * 1024,
        )
        self.domains[definition.uuid] = definition, status
        if self.fail_after_define:
            raise VirtualizationError("connection lost after define")
        return status

    def status(self, vm: UUID, owner: UUID) -> VMStatus:
        if vm not in self.domains:
            raise DomainNotFound(str(vm))
        definition, status = self.domains[vm]
        assert definition.owner_id == owner
        return status

    def check_inventory(
        self, vm: UUID, owner: UUID, overlay: str, seed: str, resource_key: str | None
    ) -> None:
        self.status(vm, owner)
        definition, _ = self.domains[vm]
        if (
            definition.overlay.key,
            definition.seed.key,
            self.resource_volumes.get(vm),
        ) != (overlay, seed, resource_key):
            raise VirtualizationError("Volume identity changed")

    def resource_media(self, vm: UUID, owner: UUID) -> str | None:
        self.status(vm, owner)
        return self.resource_volumes.get(vm)

    def ensure_resources_editable(self, vm: UUID, owner: UUID) -> None:
        if self.status(vm, owner).active:
            raise VirtualizationError("Shut down the VM before changing resource media")

    def import_resource_iso(self, pool: str, vm: UUID, iso: Path) -> Volume:
        return self._volume(f"focusvm-{vm}-resources-{iso.name}")

    def set_resource_media(
        self, vm: UUID, owner: UUID, volume: Volume | None
    ) -> VMStatus:
        self.resource_volumes[vm] = None if volume is None else volume.key
        updated = replace(
            self.status(vm, owner), resource_media_attached=volume is not None
        )
        self.domains[vm] = self.domains[vm][0], updated
        if self.fail_after_resource_change:
            raise VirtualizationError("connection lost after media change")
        return updated

    def start(self, vm: UUID, owner: UUID, base_key: str) -> VMStatus:
        base = self.domains[vm][0].base
        assert base is not None and base.key == base_key
        self.start_calls += 1
        return self._change_state(vm, owner, state="running", active=True)

    def stop(self, vm: UUID, owner: UUID, timeout: float) -> VMStatus:
        return self._change_state(vm, owner, state="shut-off", active=False)

    def _change_state(
        self, vm: UUID, owner: UUID, *, state: str, active: bool
    ) -> VMStatus:
        status = replace(self.status(vm, owner), state=state, active=active)
        definition, _ = self.domains[vm]
        self.domains[vm] = definition, status
        return status


@pytest.fixture
def vm_environment(tmp_path: Path) -> Generator[tuple[VMService, _Backend, ImageInput]]:
    backend = _Backend(tmp_path / "pool")
    source = ImageInput(
        *(tmp_path / name for name in ("base", "manifest", "signature", "keyring"))
    )
    image = VerifiedImage(source.image, "1" * 64, 16 * GIB, "test-image")
    service = VMService(tmp_path / "state", tmp_path / "cache")
    with (
        patch("focusvm.vm.connect", backend.connect),
        patch("focusvm.vm.stage_image", return_value=nullcontext(image)),
        patch("focusvm.vm.build_seed", return_value=nullcontext(tmp_path / "seed.iso")),
    ):
        yield service, backend, source


def _create(service: VMService, source: ImageInput, name: str = "dev") -> VMStatus:
    return service.create(name, source, "guest-only-password", "work", VMDefaults())


def test_create_and_select_use_durable_volume_receipts(
    vm_environment: tuple[VMService, _Backend, ImageInput],
) -> None:
    service, backend, source = vm_environment
    status = _create(service, source)
    (record,) = service.list_records()

    assert record.uuid == status.uuid
    assert record.phase == "ready"
    assert record.base_key and record.overlay_key and record.seed_key
    assert "guest-only-password" not in service.path.read_text()
    assert service.path.stat().st_mode & 0o777 == 0o600
    assert service.status(None) == status
    assert backend.read_modes[-1] is True
    assert service.start(status.uuid).active
    assert not service.stop(None, 10).active
    assert _create(service, source).uuid == status.uuid
    assert len(backend.overlays) == 1


def test_retry_reconciles_definition_created_before_connection_loss(
    vm_environment: tuple[VMService, _Backend, ImageInput],
) -> None:
    service, backend, source = vm_environment
    backend.fail_after_define = True

    with pytest.raises(VMStateError, match="Retry with the same name"):
        _create(service, source)
    (record,) = service.list_records()
    assert record.phase == "pending"

    assert _create(service, source).uuid == record.uuid
    assert service.list_records()[0].phase == "ready"
    assert len(backend.overlays) == 1


def test_start_reconciles_definition_created_before_connection_loss(
    vm_environment: tuple[VMService, _Backend, ImageInput],
) -> None:
    service, backend, source = vm_environment
    backend.fail_after_define = True
    with pytest.raises(VMStateError, match="Retry with the same name"):
        _create(service, source)
    (record,) = service.list_records()
    assert record.phase == "pending"

    assert service.start(record.uuid).active
    assert service.list_records()[0].phase == "ready"
    assert len(backend.overlays) == 1


def test_retry_preserves_partial_volumes_and_uses_new_identity(
    vm_environment: tuple[VMService, _Backend, ImageInput],
) -> None:
    service, backend, source = vm_environment
    backend.fail_seed = True
    with pytest.raises(VMStateError, match="seed upload interrupted"):
        _create(service, source)

    (record,) = service.list_records()
    assert record.phase == "pending"
    assert record.overlay_key and record.seed_key is None

    backend.fail_seed = False
    status = _create(service, source)
    previous, current = service.list_records()
    assert previous.phase == "abandoned"
    assert current.phase == "ready"
    assert previous.overlay_key != current.overlay_key
    assert status.uuid != record.uuid
    assert len(backend.overlays) == 2
    assert service.status(None).uuid == status.uuid


def test_missing_ready_domain_is_not_recreated(
    vm_environment: tuple[VMService, _Backend, ImageInput],
) -> None:
    service, backend, source = vm_environment
    original = _create(service, source)
    del backend.domains[original.uuid]

    with pytest.raises(VMStateError, match="missing from libvirt"):
        _create(service, source)
    assert len(backend.overlays) == 1
    assert service.list_records()[0].phase == "ready"


def test_rejects_unmanaged_or_changed_volume_before_start(
    vm_environment: tuple[VMService, _Backend, ImageInput],
) -> None:
    service, backend, source = vm_environment
    original = _create(service, source)
    definition, status = backend.domains[original.uuid]
    path = backend.root / "external.qcow2"
    backend.domains[original.uuid] = (
        replace(definition, overlay=Volume(path.name, str(path), path)),
        status,
    )

    with pytest.raises(VMInputError, match="absent from"):
        service.start(uuid4())
    with pytest.raises(VirtualizationError, match="identity changed"):
        service.start(original.uuid)
    assert backend.start_calls == 0


def test_resource_change_recovers_after_definition_was_written(
    vm_environment: tuple[VMService, _Backend, ImageInput], tmp_path: Path
) -> None:
    service, backend, source = vm_environment
    vm = _create(service, source).uuid
    backend.fail_after_resource_change = True

    with pytest.raises(VirtualizationError, match="connection lost"):
        service.attach_resources(vm, tmp_path / "docs.iso")
    (pending,) = service.list_records()
    assert pending.resource_key is None and pending.resource_change is not None
    assert service.status(vm).resource_media_attached
    assert service.list_records()[0] == pending

    backend.fail_after_resource_change = False
    assert service.start(vm).resource_media_attached
    (ready,) = service.list_records()
    assert ready.resource_key == backend.resource_volumes[vm]
    assert ready.resource_change is None
    service.stop(vm, 10)
    assert not service.eject_resources(vm).resource_media_attached
    assert service.list_records()[0].resource_key is None
    assert not service.eject_resources(vm).resource_media_attached


def test_resource_change_refuses_running_guest_and_external_drift(
    vm_environment: tuple[VMService, _Backend, ImageInput], tmp_path: Path
) -> None:
    service, backend, source = vm_environment
    vm = _create(service, source).uuid
    service.start(vm)
    with pytest.raises(VirtualizationError, match="Shut down"):
        service.attach_resources(vm, tmp_path / "docs.iso")

    service.stop(vm, 10)
    service.attach_resources(vm, tmp_path / "docs.iso")
    backend.resource_volumes[vm] = "different-media"
    with pytest.raises(VirtualizationError, match="identity changed"):
        service.start(vm)
    assert backend.start_calls == 1


def test_existing_v1_inventory_without_resources_remains_readable(
    vm_environment: tuple[VMService, _Backend, ImageInput],
) -> None:
    service, _, source = vm_environment
    vm = _create(service, source)
    document: dict[str, list[dict[str, object]]] = json.loads(service.path.read_text())
    document["records"][0].pop("resource_key")
    document["records"][0].pop("resource_change")
    service.path.write_text(json.dumps(document), encoding="utf-8")

    assert service.status(vm.uuid) == vm
