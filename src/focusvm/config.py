import json
import os
import tomllib
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Self, cast
from uuid import UUID

from .local_state import PrivateFileError, atomic_write, file_lock, open_owned_file
from .profiles import PROFILES, ProfileId

SCHEMA_VERSION = 1
MAX_TOML_INTEGER = 2**63 - 1

type Theme = Literal["system", "light", "dark"]
THEMES: tuple[Theme, ...] = ("system", "light", "dark")


class ConfigError(ValueError):
    """Settings could not be validated."""


class ConfigExistsError(ConfigError):
    """Initialization would replace existing settings."""


@dataclass(frozen=True, slots=True)
class ConfigPaths:
    config_dir: Path
    state_dir: Path
    cache_dir: Path
    data_dir: Path

    @property
    def config_file(self) -> Path:
        return self.config_dir / "config.toml"

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
        home: Path | None = None,
    ) -> Self:
        env = os.environ if environment is None else environment
        user_home = Path.home() if home is None else home

        def directory(variable: str, fallback: str) -> Path:
            configured = Path(env.get(variable, ""))
            base = configured if configured.is_absolute() else user_home / fallback
            return base / "focusvm"

        return cls(
            config_dir=directory("XDG_CONFIG_HOME", ".config"),
            state_dir=directory("XDG_STATE_HOME", ".local/state"),
            cache_dir=directory("XDG_CACHE_HOME", ".cache"),
            data_dir=directory("XDG_DATA_HOME", ".local/share"),
        )


@dataclass(frozen=True, slots=True)
class ApplicationSettings:
    storage_pool: str
    theme: Theme = "system"
    default_profile: ProfileId = "deep-work"
    default_vm: UUID | None = None

    def __post_init__(self) -> None:
        _pool_name(self.storage_pool)
        _theme(self.theme)
        _profile(self.default_profile)


@dataclass(frozen=True, slots=True)
class VMDefaults:
    cpus: int = 2
    memory_mib: int = 4096
    disk_gib: int = 32

    def __post_init__(self) -> None:
        _positive_integer(self.cpus, "vm_defaults.cpus")
        _positive_integer(self.memory_mib, "vm_defaults.memory_mib")
        _positive_integer(self.disk_gib, "vm_defaults.disk_gib")


@dataclass(frozen=True, slots=True)
class Settings:
    application: ApplicationSettings
    vm_defaults: VMDefaults = field(default_factory=VMDefaults)

    @classmethod
    def for_pool(cls, storage_pool: str) -> Self:
        return cls(application=ApplicationSettings(storage_pool=storage_pool))


class ConfigStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> Settings:
        try:
            with open(self.path, "rb", opener=open_owned_file) as stream:
                document: dict[str, object] = tomllib.load(stream)
        except (tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
            raise ConfigError(f"Invalid TOML: {error}") from error
        except PrivateFileError as error:
            raise ConfigError(str(error)) from error
        return _parse_settings(document)

    def initialize(self, settings: Settings) -> bool:
        """Create settings and return whether a new file was written."""
        with self._lock():
            try:
                existing = self.load()
            except FileNotFoundError:
                self._write(settings)
                return True

            if existing != settings:
                raise ConfigExistsError(f"Settings already exist at {self.path}")
            return False

    def update(self, transform: Callable[[Settings], Settings]) -> Settings:
        """Apply a settings change to the latest committed configuration."""
        with self._lock():
            current = self.load()
            updated = transform(current)
            if updated != current:
                self._write(updated)
            return updated

    @contextmanager
    def _lock(self) -> Generator[None]:
        try:
            with file_lock(self.path):
                yield
        except PrivateFileError as error:
            raise ConfigError(str(error)) from error

    def _write(self, settings: Settings) -> None:
        atomic_write(self.path, _format_settings(settings))


def _parse_settings(document: Mapping[str, object]) -> Settings:
    version = document.get("schema_version")
    if type(version) is not int:
        raise ConfigError("schema_version must be an integer")
    if version != SCHEMA_VERSION:
        raise ConfigError(
            f"Unsupported schema_version {version}. Expected {SCHEMA_VERSION}."
        )

    _check_keys(document, {"schema_version", "application", "vm_defaults"}, "root")
    application = _table(document, "application")
    _check_keys(
        application,
        {"storage_pool", "theme", "default_profile", "default_vm"},
        "application",
    )
    vm = _table(document, "vm_defaults")
    _check_keys(vm, {"cpus", "memory_mib", "disk_gib"}, "vm_defaults")

    defaults = Settings.for_pool(_pool_name(application.get("storage_pool")))
    return Settings(
        application=ApplicationSettings(
            storage_pool=defaults.application.storage_pool,
            theme=_theme(application.get("theme", defaults.application.theme)),
            default_profile=_profile(
                application.get("default_profile", defaults.application.default_profile)
            ),
            default_vm=_vm_uuid(application.get("default_vm")),
        ),
        vm_defaults=VMDefaults(
            cpus=_positive_integer(
                vm.get("cpus", defaults.vm_defaults.cpus), "vm_defaults.cpus"
            ),
            memory_mib=_positive_integer(
                vm.get("memory_mib", defaults.vm_defaults.memory_mib),
                "vm_defaults.memory_mib",
            ),
            disk_gib=_positive_integer(
                vm.get("disk_gib", defaults.vm_defaults.disk_gib),
                "vm_defaults.disk_gib",
            ),
        ),
    )


def _check_keys(table: Mapping[str, object], allowed: set[str], name: str) -> None:
    unknown = sorted(table.keys() - allowed)
    if unknown:
        raise ConfigError(f"Unknown settings in {name}: {', '.join(unknown)}")


def _table(document: Mapping[str, object], name: str) -> Mapping[str, object]:
    value = document.get(name)
    if not isinstance(value, dict):
        raise ConfigError(f"[{name}] must be a TOML table")
    # tomllib guarantees string keys in each table.
    return cast("dict[str, object]", value)


def _pool_name(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or not value.isprintable()
        or "/" in value
        or value in {".", ".."}
    ):
        raise ConfigError(
            "application.storage_pool must be a nonempty libvirt pool name"
        )
    return value


def _theme(value: object) -> Theme:
    for theme in THEMES:
        if value == theme:
            return theme
    raise ConfigError("application.theme must be system, light, or dark")


def _profile(value: object) -> ProfileId:
    for profile in PROFILES:
        if value == profile:
            return profile
    raise ConfigError(
        f"application.default_profile must be one of {', '.join(PROFILES)}"
    )


def _vm_uuid(value: object) -> UUID | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            return UUID(value)
        except ValueError as error:
            raise ConfigError("application.default_vm must be a domain UUID") from error
    raise ConfigError("application.default_vm must be a domain UUID")


def _positive_integer(value: object, name: str) -> int:
    if type(value) is not int or not 0 < value <= MAX_TOML_INTEGER:
        raise ConfigError(f"{name} must be a positive 64-bit integer")
    return value


def _format_settings(settings: Settings) -> str:
    application = settings.application
    vm = settings.vm_defaults
    # For validated printable strings, JSON quoting also produces a TOML basic string.
    lines = [
        f"schema_version = {SCHEMA_VERSION}",
        "",
        "[application]",
        f"theme = {json.dumps(application.theme)}",
        f"default_profile = {json.dumps(application.default_profile)}",
        f"storage_pool = {json.dumps(application.storage_pool, ensure_ascii=False)}",
    ]
    if application.default_vm is not None:
        lines.append(f'default_vm = "{application.default_vm}"')
    lines.extend(
        [
            "",
            "[vm_defaults]",
            f"cpus = {vm.cpus}",
            f"memory_mib = {vm.memory_mib}",
            f"disk_gib = {vm.disk_gib}",
            "",
        ]
    )
    return "\n".join(lines)
