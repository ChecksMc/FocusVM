"""Verify a signed Fedora guest and stage its GitHub release assets."""

import argparse
import hashlib
import json
import re
import shutil
import stat
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

from focusvm.provisioning import (
    COPY_CHUNK_BYTES,
    MAX_KEYRING_BYTES,
    MAX_METADATA_BYTES,
    ImageInput,
    ProvisioningUnavailable,
    stage_image,
)
from guest.build import (
    FEDORA_KEY,
    FEDORA_TREE,
    HERE,
    ISO_NAME,
    ISO_SHA256,
    BuildError,
    run,
    sha256,
)

GITHUB_ASSET_LIMIT = 2 * 1024**3
RPM_ID = re.compile(
    r"(?P<name>[A-Za-z0-9_.+-]+)-[^\s-]+-[^\s-]+\.(?P<arch>[A-Za-z0-9_]+|\(none\))"
)


def prepare_release(
    *,
    build_dir: Path,
    compressed_image: Path,
    signature: Path,
    keyring: Path,
    publisher_fingerprint: str,
    version: str,
    output: Path,
) -> None:
    """Stage verified assets under a new directory for manual publication."""
    if not re.fullmatch(r"(?:[0-9A-Fa-f]{40}|[0-9A-Fa-f]{64})", publisher_fingerprint):
        raise BuildError("Provide the publisher's full primary key fingerprint")
    fingerprint = publisher_fingerprint.upper()
    if output.exists() or output.is_symlink():
        raise BuildError(f"Choose a new output directory: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".focusvm-guest-release-", dir=output.parent) as tmp:
        work = Path(tmp).absolute()
        assets = work / "assets"
        assets.mkdir(mode=0o700)
        sources = {
            "build-inputs.json": (build_dir / "build-inputs.json", MAX_METADATA_BYTES),
            "guest.json": (build_dir / "guest.json", MAX_METADATA_BYTES),
            "guest.qcow2.zst": (compressed_image, GITHUB_ASSET_LIMIT - 1),
            "guest.sig": (signature, MAX_METADATA_BYTES),
            "packages.txt": (build_dir / "packages.txt", MAX_KEYRING_BYTES),
            "publisher.gpg": (keyring, MAX_KEYRING_BYTES),
        }
        for name, (source, limit) in sources.items():
            info = source.lstat()
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= limit:
                raise BuildError(
                    f"Expected a regular asset of 1 to {limit} bytes: {source}"
                )
            target = assets / name
            shutil.copyfile(source, target)
            if not 0 < target.stat().st_size <= limit:
                raise BuildError(
                    f"The staged asset size changed outside its limit: {source}"
                )

        image_input = ImageInput(
            build_dir / "guest.qcow2",
            assets / "guest.json",
            assets / "guest.sig",
            assets / "publisher.gpg",
        )
        _verify_signer(image_input, fingerprint, work)
        _verify_build_inputs(assets)
        with stage_image(image_input, staging_dir=work) as image:
            if image.version != version:
                raise BuildError(
                    "The signed manifest does not match the release version"
                )
            _verify_compressed(
                assets / "guest.qcow2.zst", image.sha256, image.path.stat().st_size
            )

        (assets / "SHA256SUMS").write_text(
            "".join(f"{sha256(assets / name)}  {name}\n" for name in sources),
            encoding="utf-8",
        )
        output.mkdir(mode=0o700)
        try:
            assets.replace(output)
        except OSError:
            output.rmdir()
            raise


def _verify_signer(source: ImageInput, fingerprint: str, homedir: Path) -> None:
    listing = run(
        "gpg",
        "--no-options",
        "--batch",
        "--homedir",
        homedir,
        "--with-colons",
        "--import-options",
        "show-only",
        "--dry-run",
        "--import",
        source.keyring,
    )
    records = [line.split(":") for line in listing.splitlines()]
    keys = [fields for fields in records if fields[0] in {"pub", "sec"}]
    if (
        len(keys) != 1
        or keys[0][0] != "pub"
        or keys[0][1] in {"e", "r"}
        or any(fields[0] == "ssb" for fields in records)
    ):
        raise BuildError("Export one current OpenPGP public key for the publisher")
    fingerprints = [fields[9] for fields in records if fields[0] == "fpr"]
    if not fingerprints or fingerprints[0] != fingerprint:
        raise BuildError(
            "The supplied public key does not match the publisher fingerprint"
        )
    status = run(
        "gpgv",
        "--homedir",
        homedir,
        "--keyring",
        source.keyring,
        "--status-fd",
        "1",
        source.signature,
        source.manifest,
    )
    tags = [
        line.removeprefix("[GNUPG:] ").split()
        for line in status.splitlines()
        if line.startswith("[GNUPG:] ")
    ]
    tags = [fields for fields in tags if fields]
    signatures = [fields for fields in tags if fields[0] == "VALIDSIG"]
    if (
        len(signatures) != 1
        or len(signatures[0]) != 11
        or signatures[0][-1] != fingerprint
        or not any(fields[0] == "GOODSIG" for fields in tags)
    ):
        raise BuildError("The manifest needs one valid signature by the publisher")


def _verify_build_inputs(assets: Path) -> None:
    inputs: object = json.loads(
        (assets / "build-inputs.json").read_text(encoding="utf-8")
    )
    if not isinstance(inputs, dict):
        raise BuildError("Expected a JSON object for the guest build inputs")
    fields = cast("dict[str, object]", inputs)
    repository = fields.get("repository")
    if not isinstance(repository, str) or not repository:
        raise BuildError("The guest build inputs lack the package repository")
    recipe = (HERE / "fedora-44.ks").read_text(encoding="utf-8")
    kickstart = recipe.replace(f"url --url={FEDORA_TREE}", f"url --url={repository}")
    expected = {
        "installer": ISO_NAME,
        "installer_sha256": ISO_SHA256,
        "fedora_signing_key": FEDORA_KEY,
        "kickstart_sha256": hashlib.sha256(kickstart.encode()).hexdigest(),
        "verification_script_sha256": sha256(HERE / "verify.sh"),
    }
    for name, value in expected.items():
        if fields.get(name) != value:
            raise BuildError(
                f"The guest build input {name} does not match this checkout"
            )
    version = fields.get("qemu_img")
    if not isinstance(version, str) or not version.startswith("qemu-img version "):
        raise BuildError("The guest build inputs lack the qemu-img version")
    packages = (assets / "packages.txt").read_text(encoding="utf-8").splitlines()
    names: set[str] = set()
    for package in packages:
        match = RPM_ID.fullmatch(package)
        if match is None or (
            match["arch"] == "(none)" and match["name"] != "gpg-pubkey"
        ):
            raise BuildError(
                f"Invalid RPM identifier in the guest package list: {package}"
            )
        names.add(match["name"])
    if any(name.startswith(("fedora-logos", "fedora-release")) for name in names):
        raise BuildError("The guest package inventory includes Fedora branding")
    if not {"generic-logos", "generic-release"} <= names:
        raise BuildError(
            "The guest package inventory lacks generic release or logo RPMs"
        )


def _verify_compressed(archive: Path, digest: str, expected_size: int) -> None:
    zstd = shutil.which("zstd")
    if zstd is None:
        raise BuildError("Required command is missing: zstd")
    with archive.open("rb") as stream:
        if stream.read(4) != bytes.fromhex("28b52ffd"):
            raise BuildError("Expected a Zstandard frame in the compressed asset")
    actual = hashlib.sha256()
    size = 0
    with subprocess.Popen(  # noqa: S603 - argv goes directly to the installed tool.
        [zstd, "--decompress", "--stdout", "--quiet", "--", str(archive)],
        stdout=subprocess.PIPE,
    ) as process:
        assert process.stdout is not None  # noqa: S101 - the pipe is requested above.
        while chunk := process.stdout.read(COPY_CHUNK_BYTES):
            size += len(chunk)
            if size > expected_size:
                process.kill()
                raise BuildError(
                    "The compressed asset expands beyond the verified guest"
                )
            actual.update(chunk)
        if process.wait() or size != expected_size or actual.hexdigest() != digest:
            raise BuildError("The compressed asset does not match the signed guest")


class Arguments(argparse.Namespace):
    build_dir: Path
    compressed_image: Path
    signature: Path
    keyring: Path
    publisher_fingerprint: str
    version: str
    output: Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-dir", type=Path, required=True)
    parser.add_argument("--compressed-image", type=Path, required=True)
    parser.add_argument("--signature", type=Path, required=True)
    parser.add_argument("--keyring", type=Path, required=True)
    parser.add_argument("--publisher-fingerprint", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(namespace=Arguments())
    try:
        prepare_release(**vars(args))
    except (
        BuildError,
        ProvisioningUnavailable,
        OSError,
        ValueError,
        subprocess.SubprocessError,
    ) as error:
        parser.exit(1, f"Guest release preparation failed: {error}\n")
    print(f"Verified guest assets: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
