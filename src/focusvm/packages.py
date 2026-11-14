"""Prepare explicitly chosen Python wheels for offline guest installation."""

import hashlib
import os
import re
import stat
from collections.abc import Sequence
from email.parser import BytesHeaderParser
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import BadZipFile, ZipFile
from zlib import error as ZipDecompressionError

from .resources import ResourceLibrary

_WHEEL_NAME = re.compile(
    r"(?P<project>[A-Za-z0-9][A-Za-z0-9_.]*)-"
    r"(?P<version>[A-Za-z0-9][A-Za-z0-9_.+!]*)-"
    r"(?:[0-9][A-Za-z0-9_.]*-)?"
    r"[A-Za-z0-9_.]+-[A-Za-z0-9_.]+-[A-Za-z0-9_.]+\.whl"
)
_METADATA_LIMIT = 256 * 1024


class PackageInputError(ValueError):
    """A selected file cannot be used to prepare offline package media."""


def build_wheel_media(wheels: Sequence[Path], output: Path) -> Path:
    """Put a complete, user-selected wheel set on a read-only reference disc."""
    if not wheels:
        raise PackageInputError("Select one or more local Python wheel files")
    if output.suffix.lower() != ".iso":
        raise PackageInputError("Use an output filename ending in .iso")
    output = output.absolute()
    if os.path.lexists(output):
        raise PackageInputError(f"Output already exists: {output}")

    projects: set[str] = set()
    selected: list[tuple[Path, str]] = []
    for wheel in wheels:
        filename = _WHEEL_NAME.fullmatch(wheel.name)
        if filename is None:
            raise PackageInputError(f"Invalid Python wheel filename: {wheel.name}")
        project = _normalized_name(filename["project"])
        if project in projects:
            raise PackageInputError(f"Select only one wheel for {project}")
        digest = _validate_wheel(wheel, filename["project"], filename["version"])
        selected.append((wheel, digest))
        projects.add(project)

    with TemporaryDirectory(prefix=".focusvm-wheels-", dir=output.parent) as temporary:
        library = ResourceLibrary(Path(temporary))
        ids: list[str] = []
        for wheel, digest in selected:
            resource = library.add(wheel)
            if resource.sha256 != digest:
                raise PackageInputError(
                    f"Wheel changed during preparation: {wheel.name}"
                )
            ids.append(resource.id)
        return library.build_iso(ids, output)


def _normalized_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _validate_wheel(wheel: Path, project: str, version: str) -> str:
    info = f"{project}-{version}.dist-info"
    try:
        with open(wheel, "rb", opener=_open_regular) as stream:
            initial = os.fstat(stream.fileno())
            with ZipFile(stream) as archive:
                names = set(archive.namelist())
                if len(names) != len(archive.infolist()) or any(
                    name.startswith(("/", "\\")) or ".." in name.split("/")
                    for name in names
                ):
                    raise PackageInputError(
                        f"Wheel has invalid archive paths: {wheel.name}"
                    )
                metadata = _read_headers(archive, f"{info}/METADATA")
                wheel_headers = _read_headers(archive, f"{info}/WHEEL")
                archive.getinfo(f"{info}/RECORD")
                if (
                    _normalized_name(metadata.get("name", ""))
                    != _normalized_name(project)
                    or not metadata.get("version")
                    or not re.fullmatch(
                        r"1\.\d+", wheel_headers.get("wheel-version", "")
                    )
                ):
                    raise PackageInputError(
                        f"Wheel metadata does not match {wheel.name}"
                    )
            stream.seek(0)
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
            final = os.fstat(stream.fileno())
            if (initial.st_size, initial.st_mtime_ns, initial.st_ctime_ns) != (
                final.st_size,
                final.st_mtime_ns,
                final.st_ctime_ns,
            ):
                raise PackageInputError(
                    f"Wheel changed during validation: {wheel.name}"
                )
            return digest
    except (
        OSError,
        BadZipFile,
        KeyError,
        RuntimeError,
        ZipDecompressionError,
    ) as error:
        raise PackageInputError(
            f"Could not read Python wheel {wheel.name}: {error}"
        ) from error


def _read_headers(archive: ZipFile, member: str) -> dict[str, str]:
    with archive.open(member) as stream:
        raw = stream.read(_METADATA_LIMIT + 1)
    if len(raw) > _METADATA_LIMIT:
        raise PackageInputError(f"Wheel metadata is too large: {member}")
    message = BytesHeaderParser().parsebytes(raw)
    return {key.lower(): str(value) for key, value in message.items()}


def _open_regular(path: str, flags: int) -> int:
    descriptor = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise PackageInputError(f"Expected a local regular file: {path}")
    except (OSError, PackageInputError):
        os.close(descriptor)
        raise
    return descriptor
