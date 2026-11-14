"""Discover local storage and validate first-run FocusVM settings."""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .config import ConfigExistsError, ConfigPaths, ConfigStore, Settings
from .diagnostics import Diagnostic, diagnose
from .virtualization import VirtualizationUnavailable, connect


@dataclass(frozen=True, slots=True)
class SetupResult:
    path: Path
    storage_pool: str
    created: bool
    checks: tuple[Diagnostic, ...]

    @property
    def ready(self) -> bool:
        return _checks_pass(self.checks)


def setup_pools() -> tuple[str, ...]:
    """Query usable pools without changing the host or requiring write access."""
    with connect(read_only=True) as backend:
        return backend.available_storage_pools()


def preferred_pool(pools: Sequence[str]) -> str | None:
    for name in ("focusvm", "default"):
        if name in pools:
            return name
    return pools[0] if len(pools) == 1 else None


def setup_host(paths: ConfigPaths, requested_pool: str | None = None) -> SetupResult:
    store = ConfigStore(paths.config_file)
    existing = _existing_settings(store)
    if requested_pool is not None:
        Settings.for_pool(requested_pool)
    if existing is not None:
        pool = _retained_pool(existing, requested_pool)
    elif requested_pool is not None:
        pool = requested_pool
    else:
        pool = _select_storage_pool()

    checks = tuple(diagnose(paths, pool))
    created = False
    if existing is None and _checks_pass(checks):
        try:
            created = store.initialize(Settings.for_pool(pool))
        except ConfigExistsError:
            # A simultaneous setup could have saved preferences for the same pool.
            _retained_pool(store.load(), pool)
    return SetupResult(store.path, pool, created, checks)


def _select_storage_pool() -> str:
    pools = setup_pools()
    if pool := preferred_pool(pools):
        return pool
    if not pools:
        raise VirtualizationUnavailable(
            "No active directory storage pool has free space. "
            "Prepare one in libvirt, then run focusvm setup again."
        )
    names = ", ".join(repr(name) for name in pools)
    raise VirtualizationUnavailable(
        f"Choose a storage pool with focusvm setup --storage-pool NAME. "
        f"Available pools are {names}."
    )


def _existing_settings(store: ConfigStore) -> Settings | None:
    try:
        return store.load()
    except FileNotFoundError:
        return None


def _retained_pool(settings: Settings, requested_pool: str | None) -> str:
    pool = settings.application.storage_pool
    if requested_pool is not None and requested_pool != pool:
        raise ConfigExistsError(
            f"FocusVM is already configured for {pool!r}. To switch pools, "
            "select Storage pool in the desktop manager’s Settings page."
        )
    return pool


def _checks_pass(checks: Sequence[Diagnostic]) -> bool:
    return all(check.status != "error" for check in checks) and any(
        check.name == "storage_pool" and check.status == "ok" for check in checks
    )
