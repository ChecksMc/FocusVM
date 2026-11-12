import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from focusvm import provisioning
from focusvm.provisioning import (
    ImageInput,
    ProvisioningError,
    ProvisioningUnavailable,
    build_seed,
    stage_image,
)

IMAGE_SIZE = 1024**2


def _write_image(source: ImageInput, contents: bytes, **fields: object) -> None:
    source.image.write_bytes(contents)
    manifest: dict[str, object] = {
        "manifest_version": 1,
        "version": "test-image",
        "architecture": "x86_64",
        "firmware": "bios",
        "format": "qcow2",
        "sha256": hashlib.sha256(contents).hexdigest(),
        "virtual_size_bytes": IMAGE_SIZE,
    }
    manifest.update(fields)
    source.manifest.write_text(json.dumps(manifest), encoding="utf-8")


@pytest.fixture
def image_input(tmp_path: Path) -> ImageInput:
    source = ImageInput(
        tmp_path / "guest.qcow2",
        tmp_path / "manifest.json",
        tmp_path / "manifest.sig",
        tmp_path / "keyring.gpg",
    )
    _write_image(source, b"QFI\xfb" + (3).to_bytes(4, "big") + bytes(96))
    source.signature.write_bytes(b"detached signature")
    source.keyring.write_bytes(b"public keyring")
    return source


@pytest.fixture
def provisioning_commands(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    calls: list[tuple[str, ...]] = []

    def run(
        tool: str, *arguments: str, data: bytes | None = None, timeout: int = 30
    ) -> subprocess.CompletedProcess[bytes]:
        calls.append((tool, *arguments))
        output = b""
        if tool == "gpgv":
            assert Path(arguments[-2]).read_bytes() == b"detached signature"
            assert Path(arguments[-3]).read_bytes() == b"public keyring"
        elif tool == "qemu-img" and arguments[0] == "info":
            output = json.dumps(
                {"format": "qcow2", "virtual-size": IMAGE_SIZE}
            ).encode()
        return subprocess.CompletedProcess([tool, *arguments], 0, output, b"")

    monkeypatch.setattr(provisioning, "_run_tool", run)
    return calls


def test_verified_copy_is_private_and_removed_after_use(
    image_input: ImageInput,
    provisioning_commands: list[tuple[str, ...]],
    tmp_path: Path,
) -> None:
    contents = image_input.image.read_bytes()
    staging = tmp_path / "cache"
    with stage_image(image_input, staging_dir=staging) as image:
        assert image.version == "test-image"
        assert image.sha256 == hashlib.sha256(contents).hexdigest()
        assert image.virtual_size_bytes == IMAGE_SIZE
        assert image.path.is_relative_to(staging)
        assert image.path.stat().st_mode & 0o777 == 0o400
        assert image.path.parent.stat().st_mode & 0o777 == 0o700
        image_input.image.write_bytes(b"changed after staging")
        assert image.path.read_bytes() == contents
    assert not image.path.parent.exists()
    assert [command[:2] for command in provisioning_commands] == [
        ("gpgv", "--homedir"),
        ("qemu-img", "info"),
        ("qemu-img", "check"),
    ]


def test_bad_signature_is_rejected_before_reading_manifest_or_image(
    image_input: ImageInput, monkeypatch: pytest.MonkeyPatch
) -> None:
    def reject(tool: str, *arguments: str) -> subprocess.CompletedProcess[bytes]:
        assert tool == "gpgv"
        return subprocess.CompletedProcess([tool, *arguments], 1, b"", b"bad signature")

    image_input.image.unlink()
    image_input.manifest.write_bytes(b"invalid JSON")
    monkeypatch.setattr(provisioning, "_run_tool", reject)
    with pytest.raises(ProvisioningError, match="signature did not verify"):
        with stage_image(image_input):
            pytest.fail("The invalid signature was accepted")


@pytest.mark.parametrize("symlink", [False, True])
def test_reject_unsafe_staging_directory(
    image_input: ImageInput, tmp_path: Path, symlink: bool
) -> None:
    directory = tmp_path / "cache"
    if symlink:
        target = tmp_path / "real-cache"
        target.mkdir(mode=0o700)
        directory.symlink_to(target, target_is_directory=True)
    else:
        directory.mkdir(mode=0o700)
        directory.chmod(0o770)

    with pytest.raises(ProvisioningUnavailable, match="Staging requires a directory"):
        with stage_image(image_input, staging_dir=directory):
            pytest.fail("An unsafe staging directory was accepted")


def test_modified_image_is_rejected_before_running_qemu_img(
    image_input: ImageInput, provisioning_commands: list[tuple[str, ...]]
) -> None:
    image_input.image.write_bytes(b"modified")
    with pytest.raises(ProvisioningError, match="does not match"):
        with stage_image(image_input):
            pytest.fail("The modified image was accepted")
    assert [command[0] for command in provisioning_commands] == ["gpgv"]


@pytest.mark.parametrize(
    ("offset", "value", "message"),
    [
        (15, 1, "backing file"),
        (35, 1, "encrypted"),
        (79, 4, "external data file"),
        (79, 2, "dirty or corrupt"),
    ],
)
def test_reject_unsupported_qcow2_flags_before_running_qemu_img(
    image_input: ImageInput,
    provisioning_commands: list[tuple[str, ...]],
    offset: int,
    value: int,
    message: str,
) -> None:
    contents = bytearray(image_input.image.read_bytes())
    contents[offset] = value
    _write_image(image_input, bytes(contents))
    with pytest.raises(ProvisioningError, match=message):
        with stage_image(image_input):
            pytest.fail("The unsupported QCOW2 was accepted")
    assert [command[0] for command in provisioning_commands] == ["gpgv"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("manifest_version", True),
        ("sha256", "bad digest"),
        ("architecture", "aarch64"),
        ("firmware", "uefi"),
        ("virtual_size_bytes", False),
    ],
)
def test_reject_unsupported_manifest_fields(
    image_input: ImageInput,
    provisioning_commands: list[tuple[str, ...]],
    field: str,
    value: object,
) -> None:
    _write_image(image_input, image_input.image.read_bytes(), **{field: value})
    with pytest.raises(ProvisioningError):
        with stage_image(image_input):
            pytest.fail("The unsupported manifest was accepted")
    assert [command[0] for command in provisioning_commands] == ["gpgv"]


def test_reject_manifest_duplicate_keys(
    image_input: ImageInput, provisioning_commands: list[tuple[str, ...]]
) -> None:
    manifest = image_input.manifest.read_text(encoding="utf-8")
    image_input.manifest.write_text(
        manifest[:-1] + ', "version": "duplicate"}', encoding="utf-8"
    )
    with pytest.raises(ProvisioningError, match="duplicate keys"):
        with stage_image(image_input):
            pytest.fail("The ambiguous manifest was accepted")
    assert [command[0] for command in provisioning_commands] == ["gpgv"]


def test_reject_symlink_input(
    image_input: ImageInput, provisioning_commands: list[tuple[str, ...]]
) -> None:
    target = image_input.image.with_suffix(".original")
    image_input.image.rename(target)
    image_input.image.symlink_to(target)
    with pytest.raises(ProvisioningError, match="Cannot read guest image input"):
        with stage_image(image_input):
            pytest.fail("A symbolic link was accepted")
    assert [command[0] for command in provisioning_commands] == ["gpgv"]


def test_seed_keeps_password_out_of_arguments_and_user_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    login_phrase = "test guest login €"
    instance = uuid4()
    sha512_crypt = b"$6$salt$" + b"a" * 86

    def run(
        tool: str, *arguments: str, data: bytes | None = None
    ) -> subprocess.CompletedProcess[bytes]:
        assert all(login_phrase not in argument for argument in arguments)
        if tool == "openssl":
            assert arguments == ("passwd", "-6", "-stdin")
            assert data == login_phrase.encode() + b"\n"
            return subprocess.CompletedProcess([tool], 0, sha512_crypt + b"\n", b"")
        assert tool == "xorriso"
        assert arguments[arguments.index("-volid") + 1] == "cidata"
        metadata = Path(arguments[-2]).read_text(encoding="utf-8")
        userdata = Path(arguments[-1]).read_text(encoding="utf-8")
        assert metadata == f"instance-id: {instance}\n"
        assert userdata.startswith("#cloud-config\n")
        assert "lock_passwd: false" in userdata
        assert "ssh_pwauth: false" in userdata
        assert login_phrase not in userdata
        assert sha512_crypt.decode() in userdata
        Path(arguments[arguments.index("-output") + 1]).write_bytes(b"sample ISO")
        return subprocess.CompletedProcess([tool], 0, b"", b"")

    monkeypatch.setattr(provisioning, "_run_tool", run)
    with build_seed(instance, login_phrase) as seed:
        assert seed.stat().st_mode & 0o777 == 0o600
        assert seed.parent.stat().st_mode & 0o777 == 0o700
    assert not seed.parent.exists()


@pytest.mark.parametrize("credential", ["", "\0", "secret\nother", "é" * 129])
def test_seed_rejects_invalid_password_before_launching_tools(credential: str) -> None:
    with pytest.raises(ProvisioningError, match="guest password"):
        with build_seed(uuid4(), credential):
            pytest.fail("An invalid guest password was accepted")


def test_missing_required_host_command_is_actionable(
    image_input: ImageInput, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(image_input.image.parent))
    with pytest.raises(ProvisioningUnavailable, match="gpgv"):
        with stage_image(image_input):
            pytest.fail("The image was accepted without signature verification")


@pytest.mark.skipif(
    not shutil.which("xorriso") or not shutil.which("openssl"),
    reason="NoCloud ISO smoke check needs xorriso and openssl",
)
def test_real_seed_contains_nocloud_files(tmp_path: Path) -> None:
    instance = uuid4()
    extracted = tmp_path / "extracted"
    extracted.mkdir()
    with build_seed(instance, "test-only guest credential") as seed:
        xorriso = shutil.which("xorriso")
        assert xorriso is not None
        result = subprocess.run(
            [
                xorriso,
                "-indev",
                str(seed),
                "-osirrox",
                "on",
                "-extract",
                "/",
                str(extracted),
            ],
            capture_output=True,
            check=True,
            timeout=30,
        )
        assert b"'cidata'" in result.stderr
    assert (extracted / "meta-data").read_text(encoding="utf-8") == (
        f"instance-id: {instance}\n"
    )
    user_data = (extracted / "user-data").read_text(encoding="utf-8")
    assert 'hashed_passwd: "$6$' in user_data
    assert "test-only guest credential" not in user_data
