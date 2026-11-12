from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from errno import ELOOP
from pathlib import Path
from threading import Barrier
from unittest.mock import patch
from uuid import UUID

import pytest

from focusvm.config import (
    ConfigError,
    ConfigExistsError,
    ConfigPaths,
    ConfigStore,
    Settings,
)

VALID_TOML = """\
schema_version = 1

[application]
theme = "system"
default_profile = "deep-work"
storage_pool = "focusvm"

[vm_defaults]
cpus = 2
memory_mib = 4096
disk_gib = 32
"""


def test_xdg_paths_use_defaults_for_missing_empty_and_relative_values(
    tmp_path: Path,
) -> None:
    paths = ConfigPaths.from_environment(
        {"XDG_CONFIG_HOME": "relative/config", "XDG_CACHE_HOME": ""}, home=tmp_path
    )

    assert paths.config_file == tmp_path / ".config/focusvm/config.toml"
    assert paths.state_dir == tmp_path / ".local/state/focusvm"
    assert paths.cache_dir == tmp_path / ".cache/focusvm"
    assert paths.data_dir == tmp_path / ".local/share/focusvm"


def test_xdg_paths_use_absolute_overrides(tmp_path: Path) -> None:
    paths = ConfigPaths.from_environment(
        {
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_STATE_HOME": str(tmp_path / "state"),
            "XDG_CACHE_HOME": str(tmp_path / "cache"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
        },
        home=tmp_path / "home",
    )

    assert paths.config_file == tmp_path / "config/focusvm/config.toml"
    assert paths.state_dir == tmp_path / "state/focusvm"
    assert paths.cache_dir == tmp_path / "cache/focusvm"
    assert paths.data_dir == tmp_path / "data/focusvm"


def test_initialize_creates_private_config_and_preserves_existing_file(
    tmp_path: Path,
) -> None:
    path = tmp_path / "focusvm" / "config.toml"
    store = ConfigStore(path)
    settings = Settings.for_pool("focusvm")

    assert store.initialize(settings)
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert store.load() == settings

    original = path.read_bytes() + b"\n# User comment\n"
    path.write_bytes(original)
    assert not store.initialize(settings)
    with pytest.raises(ConfigExistsError):
        store.initialize(Settings.for_pool("other-pool"))
    assert path.read_bytes() == original


def test_load_reads_configured_values_and_optional_uuid(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    vm = UUID("c5d3f07a-c947-4594-a170-16e78db9d900")
    path.write_text(
        VALID_TOML.replace('theme = "system"', 'theme = "dark"')
        .replace("cpus = 2", "cpus = 4")
        .replace(
            'storage_pool = "focusvm"',
            f'storage_pool = "dev-pool"\ndefault_vm = "{vm}"',
        ),
        encoding="utf-8",
    )

    settings = ConfigStore(path).load()

    assert settings.application.theme == "dark"
    assert settings.application.default_profile == "deep-work"
    assert settings.application.storage_pool == "dev-pool"
    assert settings.application.default_vm == vm
    assert settings.vm_defaults.cpus == 4
    assert settings.vm_defaults.memory_mib == 4096
    assert settings.vm_defaults.disk_gib == 32


def test_pool_name_round_trips_toml_quoting(tmp_path: Path) -> None:
    store = ConfigStore(tmp_path / "config.toml")
    settings = Settings.for_pool(r'Café "images" \store')

    store.initialize(settings)

    assert store.load() == settings


def test_initialize_does_not_follow_a_symlink_config(tmp_path: Path) -> None:
    destination = tmp_path / "other.toml"
    destination.write_text(VALID_TOML, encoding="utf-8")
    path = tmp_path / "config.toml"
    path.symlink_to(destination)

    with pytest.raises(OSError) as error:
        ConfigStore(path).initialize(Settings.for_pool("focusvm"))

    assert error.value.errno == ELOOP
    assert path.is_symlink()
    assert destination.read_text(encoding="utf-8") == VALID_TOML


@pytest.mark.parametrize(
    ("original", "replacement"),
    [
        ("schema_version = 1", ""),
        ("schema_version = 1", "schema_version = 2"),
        ("schema_version = 1", "schema_version = true"),
        ("[vm_defaults]", "[unexpected_section]"),
        ('theme = "system"', 'theme = "unknown"'),
        ('default_profile = "deep-work"', 'default_profile = "unknown"'),
        ('storage_pool = "focusvm"', ""),
        ('storage_pool = "focusvm"', 'storage_pool = "../focusvm"'),
        (
            'storage_pool = "focusvm"',
            'storage_pool = "focusvm"\ndefault_vm = "not-a-uuid"',
        ),
        ("cpus = 2", "cpus = true"),
        ("memory_mib = 4096", "memory_mib = 0"),
        ("disk_gib = 32", "disk_gib = -1"),
        ("cpus = 2", "cpus = ["),
    ],
)
def test_load_rejects_invalid_toml(
    tmp_path: Path, original: str, replacement: str
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(VALID_TOML.replace(original, replacement), encoding="utf-8")

    with pytest.raises(ConfigError):
        ConfigStore(path).load()


def _add_cpu(settings: Settings) -> Settings:
    return replace(
        settings,
        vm_defaults=replace(settings.vm_defaults, cpus=settings.vm_defaults.cpus + 1),
    )


def test_update_preserves_previous_file_if_replacement_fails(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    store = ConfigStore(path)
    settings = Settings.for_pool("focusvm")
    store.initialize(settings)
    previous_contents = path.read_bytes()
    previous_entries = set(tmp_path.iterdir())

    with (
        patch(
            "focusvm.config.os.replace", side_effect=OSError("simulated write failure")
        ),
        pytest.raises(OSError, match="simulated write failure"),
    ):
        store.update(_add_cpu)

    assert path.read_bytes() == previous_contents
    assert set(tmp_path.iterdir()) == previous_entries
    assert store.load() == settings
    assert store.update(_add_cpu).vm_defaults.cpus == settings.vm_defaults.cpus + 1


def test_concurrent_updates_reload_before_writing(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    store = ConfigStore(path)
    settings = Settings.for_pool("focusvm")
    store.initialize(settings)
    count = 8
    start = Barrier(count, timeout=10)

    def update() -> int:
        start.wait()
        return ConfigStore(path).update(_add_cpu).vm_defaults.cpus

    with ThreadPoolExecutor(max_workers=count) as executor:
        updates = [executor.submit(update) for _ in range(count)]
        reported = sorted(update.result() for update in updates)

    initial = settings.vm_defaults.cpus
    assert reported == list(range(initial + 1, initial + count + 1))
    assert store.load().vm_defaults.cpus == initial + count
