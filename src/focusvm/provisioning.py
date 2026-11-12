"""Prepare verified guest images and offline first-boot media."""

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from io import BufferedReader
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
from uuid import UUID

MANIFEST_VERSION = 1
GUEST_USERNAME = "focusvm"
MAX_METADATA_BYTES = 64 * 1024
MAX_KEYRING_BYTES = 1024 * 1024
MAX_PASSWORD_BYTES = 256
MAX_VIRTUAL_SIZE_BYTES = 2**63 - 1
COPY_CHUNK_BYTES = 1024 * 1024


class ProvisioningError(ValueError):
    """A guest image, manifest, or credential cannot be used."""


class ProvisioningUnavailable(RuntimeError):
    """The host cannot perform an image or seed preparation step."""


@dataclass(frozen=True, slots=True)
class ImageInput:
    image: Path
    manifest: Path
    signature: Path
    keyring: Path


@dataclass(frozen=True, slots=True)
class VerifiedImage:
    path: Path
    sha256: str
    virtual_size_bytes: int
    version: str


@contextmanager
def stage_image(
    source: ImageInput, *, staging_dir: Path | None = None
) -> Generator[VerifiedImage]:
    """Verify local input and keep an isolated copy available until upload finishes."""
    if staging_dir is not None:
        staging_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory_info = staging_dir.lstat()
        if (
            not stat.S_ISDIR(directory_info.st_mode)
            or directory_info.st_uid != os.getuid()
            or directory_info.st_mode & 0o022
        ):
            raise ProvisioningUnavailable(
                f"Staging requires a directory owned by you and safe to write: {staging_dir}"
            )
    with TemporaryDirectory(prefix="focusvm-image-", dir=staging_dir) as temporary:
        directory = Path(temporary)
        manifest = directory / "manifest.json"
        signature = directory / "manifest.sig"
        keyring = directory / "trusted.gpg"
        for original, target, limit in (
            (source.manifest, manifest, MAX_METADATA_BYTES),
            (source.signature, signature, MAX_METADATA_BYTES),
            (source.keyring, keyring, MAX_KEYRING_BYTES),
        ):
            with _open_input(original) as stream:
                contents = stream.read(limit + 1)
            if not contents or len(contents) > limit:
                raise ProvisioningError(
                    f"Invalid guest image metadata size: {original}"
                )
            target.write_bytes(contents)

        result = _run_tool(
            "gpgv",
            "--homedir",
            str(directory),
            "--keyring",
            str(keyring),
            str(signature),
            str(manifest),
        )
        if result.returncode != 0:
            raise ProvisioningError(
                "The guest image manifest signature did not verify with the supplied keyring"
            )

        image = _read_manifest(manifest, directory / "base.qcow2")
        digest = hashlib.sha256()
        with _open_input(source.image) as base, image.path.open("xb") as staged:
            while chunk := base.read(COPY_CHUNK_BYTES):
                digest.update(chunk)
                staged.write(chunk)
        if digest.hexdigest() != image.sha256:
            raise ProvisioningError(
                "The guest image SHA-256 does not match its manifest"
            )
        _check_qcow2(image)
        image.path.chmod(0o400)
        yield image


@contextmanager
def build_seed(instance_id: UUID, password: str) -> Generator[Path]:
    """Build a private NoCloud ISO for the guest's local login."""
    try:
        encoded_password = password.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ProvisioningError("The guest password must be valid UTF-8") from error
    if not 1 <= len(encoded_password) <= MAX_PASSWORD_BYTES or any(
        character in password for character in ("\x00", "\r", "\n")
    ):
        raise ProvisioningError(
            f"The guest password must contain 1 to {MAX_PASSWORD_BYTES} UTF-8 bytes "
            "and cannot contain NUL or line breaks"
        )
    result = _run_tool(
        "openssl", "passwd", "-6", "-stdin", data=encoded_password + b"\n"
    )
    password_hash = result.stdout.strip()
    if result.returncode or not re.fullmatch(
        rb"\$6\$[./A-Za-z0-9]{1,16}\$[./A-Za-z0-9]{86}", password_hash
    ):
        raise ProvisioningUnavailable("OpenSSL could not hash the guest password")

    with TemporaryDirectory(prefix="focusvm-seed-") as temporary:
        directory = Path(temporary)
        metadata = directory / "meta-data"
        metadata.write_text(f"instance-id: {instance_id}\n", encoding="utf-8")
        userdata = directory / "user-data"
        userdata.write_text(
            "#cloud-config\n"
            "users:\n"
            f"  - name: {GUEST_USERNAME}\n"
            "    shell: /bin/bash\n"
            "    lock_passwd: false\n"
            f"    hashed_passwd: {json.dumps(password_hash.decode('ascii'))}\n"
            "disable_root: true\n"
            "ssh_pwauth: false\n",
            encoding="utf-8",
        )
        userdata.chmod(0o600)
        seed = directory / "seed.iso"
        result = _run_tool(
            "xorriso",
            "-as",
            "mkisofs",
            "-quiet",
            "-volid",
            "cidata",
            "-joliet",
            "-rock",
            "-output",
            str(seed),
            str(metadata),
            str(userdata),
        )
        if result.returncode or not seed.is_file() or seed.stat().st_size == 0:
            raise ProvisioningUnavailable(
                "xorriso could not build the NoCloud seed image"
            )
        seed.chmod(0o600)
        yield seed


def _open_input(path: Path) -> BufferedReader:
    try:
        return open(path, "rb", opener=_open_regular)
    except OSError as error:
        raise ProvisioningError(
            f"Cannot read guest image input {path}: {error.strerror}"
        ) from error


def _open_regular(path: str, flags: int) -> int:
    descriptor = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ProvisioningError(f"Expected a regular guest image input: {path}")
    except (OSError, ProvisioningError):
        os.close(descriptor)
        raise
    return descriptor


def _unique_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
    fields = dict(pairs)
    if len(fields) != len(pairs):
        raise ProvisioningError("The guest image manifest contains duplicate keys")
    return fields


def _read_manifest(path: Path, staged_image: Path) -> VerifiedImage:
    try:
        document: object = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_unique_fields
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise ProvisioningError(
            "The guest image manifest is not valid UTF-8 JSON"
        ) from error
    if not isinstance(document, dict):
        raise ProvisioningError("The guest image manifest must be a JSON object")
    manifest = cast("dict[str, object]", document)
    expected_keys = {
        "manifest_version",
        "version",
        "sha256",
        "architecture",
        "format",
        "virtual_size_bytes",
        "firmware",
    }
    if manifest.keys() != expected_keys:
        raise ProvisioningError(
            "The guest image manifest must contain the version 1 fields"
        )
    manifest_version = manifest["manifest_version"]
    if type(manifest_version) is not int or manifest_version != MANIFEST_VERSION:
        raise ProvisioningError("Unsupported guest image manifest version")
    if (manifest["architecture"], manifest["format"], manifest["firmware"]) != (
        "x86_64",
        "qcow2",
        "bios",
    ):
        raise ProvisioningError("Expected a QCOW2 image for an x86_64 BIOS guest")

    version = manifest["version"]
    if not isinstance(version, str) or not version.isprintable() or not version.strip():
        raise ProvisioningError("The guest image version must be a printable string")
    digest = manifest["sha256"]
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ProvisioningError(
            "The guest image SHA-256 must be 64 lowercase hex digits"
        )
    size = manifest["virtual_size_bytes"]
    if type(size) is not int or not 0 < size <= MAX_VIRTUAL_SIZE_BYTES:
        raise ProvisioningError(
            "The guest image virtual size must be a positive 64-bit integer"
        )
    return VerifiedImage(staged_image, digest, size, version)


def _check_qcow2(image: VerifiedImage) -> None:
    # Check the QCOW2 header before passing it to a tool that could open external files.
    with image.path.open("rb") as stream:
        header = stream.read(104)
    version = int.from_bytes(header[4:8], "big")
    if (
        header[:4] != b"QFI\xfb"
        or version not in (2, 3)
        or len(header) < (104 if version == 3 else 72)
    ):
        raise ProvisioningError("The guest image does not have a valid QCOW2 header")
    if int.from_bytes(header[8:16], "big"):
        raise ProvisioningError("The guest image cannot have a backing file")
    if int.from_bytes(header[32:36], "big"):
        raise ProvisioningError("The guest image cannot be encrypted")
    if version == 3:
        features = int.from_bytes(header[72:80], "big")
        if features & 0b100:
            raise ProvisioningError("The guest image cannot have an external data file")
        if features & 0b011:
            raise ProvisioningError("The guest image is marked dirty or corrupt")

    result = _run_tool(
        "qemu-img", "info", "--output=json", "-f", "qcow2", str(image.path)
    )
    if result.returncode:
        raise ProvisioningError("qemu-img could not read the guest image as QCOW2")
    try:
        details: object = json.loads(result.stdout)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ProvisioningUnavailable(
            "qemu-img returned invalid image metadata"
        ) from error
    if not isinstance(details, dict):
        raise ProvisioningUnavailable("qemu-img returned invalid image metadata")
    info = cast("dict[str, object]", details)
    if (
        info.get("format") != "qcow2"
        or info.get("virtual-size") != image.virtual_size_bytes
    ):
        raise ProvisioningError(
            "The guest image format or virtual size does not match its manifest"
        )
    if (
        "backing-filename" in info
        or "full-backing-filename" in info
        or info.get("encrypted")
    ):
        raise ProvisioningError("Expected a standalone, unencrypted guest image")
    result = _run_tool(
        "qemu-img", "check", "-q", "-f", "qcow2", str(image.path), timeout=300
    )
    if result.returncode:
        raise ProvisioningError("The guest image failed the qemu-img consistency check")


def _run_tool(
    tool: str, *arguments: str, data: bytes | None = None, timeout: int = 30
) -> subprocess.CompletedProcess[bytes]:
    executable = shutil.which(tool)
    if executable is None:
        raise ProvisioningUnavailable(f"Required command is missing: {tool}")
    try:
        # Executables come from the host PATH. Arguments are passed without a shell.
        return subprocess.run(  # noqa: S603
            [os.path.abspath(executable), *arguments],
            input=data,
            stdin=subprocess.DEVNULL if data is None else None,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise ProvisioningUnavailable(
            f"{tool} timed out during guest provisioning"
        ) from error
    except OSError as error:
        raise ProvisioningUnavailable(
            f"Could not run {tool}: {error.strerror}"
        ) from error
