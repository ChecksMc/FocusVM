import hashlib
import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from guest.build import FEDORA_KEY, FEDORA_TREE, HERE, ISO_NAME, ISO_SHA256, BuildError
from scripts.prepare_guest_release import GITHUB_ASSET_LIMIT, prepare_release


@dataclass
class GuestInputs:
    build: Path
    archive: Path
    keyring: Path
    fingerprint: str

    def stage(
        self,
        output: Path,
        *,
        version: str = "guest-test",
        fingerprint: str | None = None,
    ) -> None:
        prepare_release(
            build_dir=self.build,
            compressed_image=self.archive,
            signature=self.build / "guest.sig",
            keyring=self.keyring,
            publisher_fingerprint=fingerprint or self.fingerprint,
            version=version,
            output=output,
        )


def run(*command: str | Path) -> bytes:
    return subprocess.run(command, capture_output=True, check=True, timeout=30).stdout


@pytest.fixture(scope="module")
def release_inputs(tmp_path_factory: pytest.TempPathFactory) -> GuestInputs:
    tools = ("gpg", "gpgconf", "gpgv", "qemu-img", "zstd")
    if any(shutil.which(tool) is None for tool in tools):
        pytest.skip("Requires GnuPG, qemu-img, and zstd")
    build = tmp_path_factory.mktemp("guest")
    image = build / "guest.qcow2"
    run("qemu-img", "create", "-f", "qcow2", image, "1M")
    archive = build / "guest.qcow2.zst"
    run("zstd", "-q", "-o", archive, image)
    manifest = build / "guest.json"
    manifest.write_text(
        json.dumps(
            {
                "manifest_version": 1,
                "version": "guest-test",
                "architecture": "x86_64",
                "format": "qcow2",
                "firmware": "bios",
                "sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
                "virtual_size_bytes": 1024**2,
            },
        ),
        encoding="utf-8",
    )
    (build / "build-inputs.json").write_text(
        json.dumps(
            {
                "installer": ISO_NAME,
                "installer_sha256": ISO_SHA256,
                "fedora_signing_key": FEDORA_KEY,
                "repository": FEDORA_TREE,
                "kickstart_sha256": hashlib.sha256(
                    (HERE / "fedora-44.ks").read_bytes()
                ).hexdigest(),
                "verification_script_sha256": hashlib.sha256(
                    (HERE / "verify.sh").read_bytes()
                ).hexdigest(),
                "qemu_img": "qemu-img version 10",
            }
        ),
        encoding="utf-8",
    )
    (build / "packages.txt").write_text(
        "generic-logos-1.0-1.noarch\n"
        "generic-release-1.0-1.noarch\n"
        "gpg-pubkey-36f612dcf27f7d1a48a835e4dbfcf71c6d9f90a6-6786af3b.(none)\n",
        encoding="utf-8",
    )
    keyring = build / "public.gpg"
    with TemporaryDirectory(prefix="focusvm-test-gpg-") as homedir:
        gpg = ("gpg", "--no-options", "--homedir", homedir, "--batch")
        unlocked = (*gpg, "--pinentry-mode", "loopback", "--passphrase", "")
        try:
            try:
                run(
                    *unlocked,
                    "--quick-generate-key",
                    "FocusVM Test Signing",
                    "ed25519",
                    "cert",
                    "1d",
                )
            except subprocess.CalledProcessError as error:
                if b"No agent running" in error.stderr:
                    pytest.skip("GnuPG agent is unavailable")
                raise
            listing = run(*gpg, "--with-colons", "--list-keys").decode()
            primary = next(
                line.split(":")[9]
                for line in listing.splitlines()
                if line.startswith("fpr:")
            )
            run(*unlocked, "--quick-add-key", primary, "ed25519", "sign", "1d")
            run(
                *unlocked,
                "--local-user",
                primary,
                "--detach-sign",
                "--output",
                build / "guest.sig",
                manifest,
            )
            keyring.write_bytes(run(*gpg, "--export", primary))
            (build / "private-test-key.gpg").write_bytes(
                run(*unlocked, "--export-secret-keys", primary)
            )
        finally:
            run("gpgconf", "--homedir", homedir, "--kill", "gpg-agent")
    return GuestInputs(build, archive, keyring, primary)


def test_stage_signed_guest_and_upload_checksums(
    release_inputs: GuestInputs, tmp_path: Path
) -> None:
    output = tmp_path / "assets"
    release_inputs.stage(output, fingerprint=release_inputs.fingerprint.lower())
    checksums = (output / "SHA256SUMS").read_text(encoding="utf-8")
    checked: set[str] = set()
    for line in checksums.splitlines():
        digest, name = line.split("  ")
        assert hashlib.sha256((output / name).read_bytes()).hexdigest() == digest
        checked.add(name)
    assert checked == {
        "build-inputs.json",
        "guest.json",
        "guest.qcow2.zst",
        "guest.sig",
        "packages.txt",
        "publisher.gpg",
    }
    assert {p.name for p in output.iterdir()} == {*checked, "SHA256SUMS"}


def test_reject_wrong_signing_fingerprint_and_wrong_build_version(
    release_inputs: GuestInputs, tmp_path: Path
) -> None:
    output = tmp_path / "assets"
    with pytest.raises(BuildError, match="publisher fingerprint"):
        release_inputs.stage(output, fingerprint="0" * 40)
    with pytest.raises(BuildError, match="release version"):
        release_inputs.stage(output, version="wrong-build")
    assert list(tmp_path.iterdir()) == []


def test_reject_secret_keyring(release_inputs: GuestInputs, tmp_path: Path) -> None:
    output = tmp_path / "assets"
    secret_inputs = GuestInputs(
        release_inputs.build,
        release_inputs.archive,
        release_inputs.build / "private-test-key.gpg",
        release_inputs.fingerprint,
    )
    with pytest.raises(BuildError, match="public key"):
        secret_inputs.stage(output)
    assert list(tmp_path.iterdir()) == []


def test_reject_branded_inventory(release_inputs: GuestInputs, tmp_path: Path) -> None:
    build = tmp_path / "build"
    shutil.copytree(release_inputs.build, build)
    with (build / "packages.txt").open("a", encoding="utf-8") as packages:
        packages.write("fedora-release-identity-basic-44-17.noarch\n")
    inputs = GuestInputs(
        build,
        release_inputs.archive,
        release_inputs.keyring,
        release_inputs.fingerprint,
    )
    with pytest.raises(BuildError, match="Fedora branding"):
        inputs.stage(tmp_path / "assets")


def test_reject_different_archive_and_github_limit(
    release_inputs: GuestInputs, tmp_path: Path
) -> None:
    different_image = tmp_path / "different.qcow2"
    run("qemu-img", "create", "-f", "qcow2", different_image, "2M")
    different_archive = tmp_path / "different.qcow2.zst"
    run("zstd", "-q", "-o", different_archive, different_image)
    inputs = GuestInputs(
        release_inputs.build,
        different_archive,
        release_inputs.keyring,
        release_inputs.fingerprint,
    )
    output = tmp_path / "assets"
    with pytest.raises(BuildError, match="compressed asset"):
        inputs.stage(output)
    with different_archive.open("r+b") as stream:
        stream.truncate(GITHUB_ASSET_LIMIT)
    with pytest.raises(BuildError, match="regular asset"):
        inputs.stage(output)
    assert not output.exists()
