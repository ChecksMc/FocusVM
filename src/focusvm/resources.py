"""Local reference library and read-only guest media export."""

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import BinaryIO, cast
from uuid import uuid4

from .local_state import (
    PrivateFileError,
    atomic_write,
    check_private_directory,
    file_lock,
    open_private_file,
)

RESOURCE_KINDS = {
    ".pdf": "pdf",
    ".htm": "html",
    ".html": "html",
    ".md": "markdown",
    ".markdown": "markdown",
    ".zim": "zim",
    ".bundle": "git-bundle",
    ".whl": "wheel",
}
ISO_LABEL = "FOCUSVM_DOCS"
ISO_DATE = "2000010100000000"
METADATA_LIMIT = 16 * 1024
COPY_CHUNK = 1024 * 1024

type ViewerCommand = tuple[str, list[str]]


class ResourceError(ValueError):
    """A resource or library operation has invalid input."""


class ResourceUnavailable(RuntimeError):
    """The host cannot perform a resource operation."""


@dataclass(frozen=True, slots=True)
class Resource:
    id: str
    name: str
    kind: str
    size_bytes: int
    sha256: str


class ResourceLibrary:
    def __init__(self, data_dir: Path) -> None:
        self._directory = data_dir.absolute() / "resources"

    @contextmanager
    def _locked(self) -> Generator[None]:
        try:
            data = self._directory.parent
            data.mkdir(mode=0o700, parents=True, exist_ok=True)
            check_private_directory(data)
            with file_lock(self._directory / "library"):
                yield
        except PrivateFileError as error:
            raise ResourceUnavailable(str(error)) from error

    def add(self, source: Path) -> Resource:
        name = source.name
        kind = _name_kind(name)
        resource_id = uuid4().hex
        with (
            self._locked(),
            TemporaryDirectory(prefix=".import-", dir=self._directory) as temporary,
        ):
            staging = Path(temporary) / "entry"
            staging.mkdir(mode=0o700)
            staged = staging / name
            with _open_input(source) as stream:
                initial = os.fstat(stream.fileno())
                size, digest = _copy_file(stream, staged)
                final = os.fstat(stream.fileno())
            if (initial.st_size, initial.st_mtime_ns, initial.st_ctime_ns) != (
                size,
                final.st_mtime_ns,
                final.st_ctime_ns,
            ):
                raise ResourceError(f"Resource changed during import: {source}")
            resource = Resource(resource_id, name, kind, size, digest)
            atomic_write(staging / "metadata.json", json.dumps(asdict(resource)) + "\n")
            os.rename(staging, self._directory / resource_id)
            _sync_directory(self._directory)
            return resource

    def list(self) -> tuple[Resource, ...]:
        with self._locked():
            resources = (
                self._entry(path.name)[0]
                for path in self._directory.iterdir()
                if _is_resource_id(path.name)
            )
            return tuple(
                sorted(resources, key=lambda item: (item.name.casefold(), item.id))
            )

    def search(self, query: str) -> tuple[Resource, ...]:
        term = query.strip().casefold()
        return tuple(item for item in self.list() if term in item.name.casefold())

    def remove(self, resource_id: str) -> Resource:
        with self._locked():
            resource, path = self._entry(resource_id)
            with TemporaryDirectory(prefix=".remove-", dir=self._directory) as temp:
                os.rename(path.parent, Path(temp) / "entry")
                _sync_directory(self._directory)
            return resource

    def open_command(self, resource_id: str) -> ViewerCommand:
        with self._locked():
            resource, path = self._entry(resource_id)
            if resource.kind in ("git-bundle", "wheel"):
                raise ResourceError("Export this resource to use it inside the guest")
            with self._open_resource(path) as stream:
                if os.fstat(stream.fileno()).st_size != resource.size_bytes:
                    raise ResourceError(f"Resource size changed: {resource.name}")
            viewer = "kiwix-desktop" if resource.kind == "zim" else "xdg-open"
            return _tool(viewer), [str(path)]

    def build_iso(self, resource_ids: Sequence[str], output: Path) -> Path:
        """Export the selected entries as an ISO. Publish only after xorriso exits."""
        ids = sorted(set(resource_ids))
        if not ids:
            raise ResourceError("Select at least one resource to export")
        if output.suffix.lower() != ".iso":
            raise ResourceError("Use an output filename ending in .iso")
        output = output.absolute()
        if os.path.lexists(output):
            raise ResourceError(f"Output already exists: {output}")
        if output.parent.resolve().is_relative_to(self._directory.resolve()):
            raise ResourceError("Export the ISO outside the resource library")
        xorriso = _tool("xorriso")
        with (
            self._locked(),
            TemporaryDirectory(
                prefix=".focusvm-export-", dir=output.parent
            ) as temporary,
        ):
            staging = Path(temporary)
            contents = staging / "contents"
            contents.mkdir(mode=0o700)
            index = ["FocusVM resources", ""]
            for resource_id in ids:
                resource, path = self._entry(resource_id)
                target_dir = contents / resource.id
                target_dir.mkdir(mode=0o700)
                with self._open_resource(path) as stream:
                    size, digest = _copy_file(stream, target_dir / resource.name)
                if (size, digest) != (resource.size_bytes, resource.sha256):
                    raise ResourceError(f"Resource content changed: {resource.name}")
                index.append(f"{resource.id}/{resource.name}")
            (contents / "index.txt").write_text("\n".join(index) + "\n", "utf-8")
            image = staging / "resources.iso"
            _build_iso(xorriso, contents, image)
            image.chmod(0o600)
            with image.open("rb") as stream:
                os.fsync(stream.fileno())
            try:
                os.link(image, output)
            except FileExistsError as error:
                raise ResourceError(f"Output already exists: {output}") from error
            _sync_directory(output.parent)
        return output

    def _entry(self, resource_id: str) -> tuple[Resource, Path]:
        if not _is_resource_id(resource_id):
            raise ResourceError(f"Invalid resource ID: {resource_id}")
        directory = self._directory / resource_id
        try:
            info = directory.lstat()
        except FileNotFoundError as error:
            raise ResourceError(f"Unknown resource ID: {resource_id}") from error
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o022
        ):
            raise ResourceError(f"Unsafe resource directory: {resource_id}")
        try:
            with open(
                directory / "metadata.json", "rb", opener=open_private_file
            ) as stream:
                raw = stream.read(METADATA_LIMIT + 1)
            if len(raw) > METADATA_LIMIT:
                raise ValueError("Metadata file is too large")
            resource = _decode_metadata(resource_id, json.loads(raw))
        except (OSError, ValueError, RecursionError) as error:
            raise ResourceError(
                f"Invalid metadata for resource {resource_id}: {error}"
            ) from error
        return resource, directory / resource.name

    @staticmethod
    def _open_resource(path: Path) -> BinaryIO:
        try:
            return open(path, "rb", opener=open_private_file)
        except (OSError, PrivateFileError) as error:
            raise ResourceError(
                f"Could not read stored resource {path.name}: {error}"
            ) from error


def _is_resource_id(value: str) -> bool:
    return bool(re.fullmatch(r"[a-f0-9]{32}", value))


def _name_kind(name: str) -> str:
    if (
        not name.isprintable()
        or Path(name).name != name
        or name in ("", ".", "..")
        or len(os.fsencode(name)) > 255
    ):
        raise ResourceError(f"Invalid resource filename: {name!r}")
    kind = RESOURCE_KINDS.get(Path(name).suffix.lower())
    if kind is None:
        raise ResourceError(
            "Use a PDF, HTML, Markdown, ZIM, Git bundle, or Python wheel"
        )
    return kind


def _decode_metadata(resource_id: str, value: object) -> Resource:
    if not isinstance(value, dict):
        raise ValueError("Expected an object")
    data = cast("dict[str, object]", value)
    name = data.get("name")
    size = data.get("size_bytes")
    digest = data.get("sha256")
    if (
        data.get("id") != resource_id
        or not isinstance(name, str)
        or type(size) is not int
        or size < 0
        or not isinstance(digest, str)
        or not re.fullmatch(r"[a-f0-9]{64}", digest)
    ):
        raise ValueError("Malformed resource metadata")
    kind = _name_kind(name)
    if data.get("kind") != kind:
        raise ValueError("File extension and resource kind differ")
    return Resource(resource_id, name, kind, size, digest)


def _open_input(path: Path) -> BinaryIO:
    try:
        return open(path, "rb", opener=_open_regular)
    except OSError as error:
        raise ResourceError(
            f"Could not read local resource {path}: {error.strerror}"
        ) from error


def _open_regular(path: str, flags: int) -> int:
    descriptor = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ResourceError(f"Expected a local regular resource file: {path}")
    except (OSError, ResourceError):
        os.close(descriptor)
        raise
    return descriptor


def _copy_file(source: BinaryIO, destination: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with destination.open("xb") as stream:
        os.fchmod(stream.fileno(), 0o400)
        while chunk := source.read(COPY_CHUNK):
            digest.update(chunk)
            stream.write(chunk)
            size += len(chunk)
        stream.flush()
        os.fsync(stream.fileno())
    return size, digest.hexdigest()


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _tool(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise ResourceUnavailable(f"Required resource tool is missing: {name}")
    return os.path.abspath(path)


def _build_iso(xorriso: str, contents: Path, image: Path) -> None:
    arguments = [
        xorriso,
        "-no_rc",
        "-as",
        "mkisofs",
        "-quiet",
        "-iso-level",
        "3",
        "-r",
        "-V",
        ISO_LABEL,
        f"--modification-date={ISO_DATE}",
        "--set_all_file_dates",
        ISO_DATE,
        "-o",
        str(image),
        str(contents),
    ]
    try:
        result = subprocess.run(  # noqa: S603
            arguments,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=900,
            env={**os.environ, "LC_ALL": "C.UTF-8", "SOURCE_DATE_EPOCH": "946684800"},
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ResourceUnavailable(f"Could not run xorriso: {error}") from error
    if result.returncode or not image.is_file() or image.stat().st_size == 0:
        detail = result.stderr.decode(errors="replace").strip()
        raise ResourceUnavailable(f"xorriso could not build the resource ISO: {detail}")
