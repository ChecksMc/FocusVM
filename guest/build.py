#!/usr/bin/env python3
"""Build and verify a Fedora 44 FocusVM base image in transient libvirt guests."""

import argparse
import hashlib
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Protocol, cast
from urllib.parse import urlsplit
from uuid import uuid4
from xml.etree import ElementTree as ET

from focusvm.provisioning import ProvisioningError, ProvisioningUnavailable

from .smoke import SmokeConnection, SmokeError, run_guest, verify_guest

HERE = Path(__file__).resolve().parent
ISO_NAME = "Fedora-Server-netinst-x86_64-44-1.7.iso"
ISO_LABEL = "Fedora-S-dvd-x86_64-44"
ISO_SHA256 = "ae20c06bea746913cadea7d80463e13f4bf55bee4df2918111c921c674b70283"
FEDORA_KEY = "36F612DCF27F7D1A48A835E4DBFCF71C6D9F90A6"
FEDORA_TREE = (
    "https://dl.fedoraproject.org/pub/fedora/linux/releases/44/Everything/x86_64/os/"
)
GIB = 1024**3


class BuildError(RuntimeError):
    """A build input, operation, or output could not be verified."""


class Connection(SmokeConnection, Protocol):
    def close(self) -> int: ...


class Libvirt(Protocol):
    libvirtError: type[Exception]

    def open(self, uri: str) -> Connection | None: ...


class Arguments(argparse.Namespace):
    iso: Path
    checksum: Path
    fedora_keyring: Path
    output: Path
    version: str
    tree: str
    timeout: int
    sign_with: str | None


def run(*args: str | Path, timeout: int = 300) -> str:
    command = shutil.which(os.fspath(args[0]))
    if command is None:
        raise BuildError(f"Required command is missing: {args[0]}")
    result = subprocess.run(  # noqa: S603 - argv is passed directly to an installed tool.
        [command, *(os.fspath(arg) for arg in args[1:])],
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout,
    )
    if result.returncode:
        raise BuildError(
            f"{args[0]} failed: {result.stderr.strip() or result.stdout.strip()}"
        )
    return result.stdout


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify_installer(iso: Path, checksum: Path, keyring: Path, work: Path) -> None:
    metadata = work / "installer-checksums.txt"
    homedir = work / "gpgv"
    homedir.mkdir(mode=0o700)
    status = run(
        "gpgv",
        "--homedir",
        homedir,
        "--keyring",
        keyring,
        "--status-fd",
        "1",
        "--output",
        metadata,
        checksum,
    )
    signatures = [
        line.split()
        for line in status.splitlines()
        if line.startswith("[GNUPG:] VALIDSIG ")
    ]
    if not any(
        fields[2] == FEDORA_KEY or fields[-1] == FEDORA_KEY for fields in signatures
    ):
        raise BuildError(
            "The installer checksum was not signed by the pinned Fedora 44 key"
        )
    expected = f"SHA256 ({ISO_NAME}) = {ISO_SHA256}"
    if expected not in metadata.read_text(encoding="utf-8").splitlines():
        raise BuildError(
            "The signed checksum does not match the pinned Fedora 44 installer"
        )
    if sha256(iso) != ISO_SHA256:
        raise BuildError(
            "The Fedora installer ISO digest does not match its signed checksum"
        )


def installer_xml(iso: Path, work: Path) -> str:
    instance = uuid4()
    domain = ET.Element("domain", {"type": "kvm"})
    ET.SubElement(domain, "name").text = f"focusvm-build-{instance.hex[:12]}"
    ET.SubElement(domain, "uuid").text = str(instance)
    ET.SubElement(domain, "memory", {"unit": "MiB"}).text = "4096"
    ET.SubElement(domain, "vcpu").text = "4"
    guest_os = ET.SubElement(domain, "os")
    ET.SubElement(guest_os, "type", {"arch": "x86_64", "machine": "q35"}).text = "hvm"
    ET.SubElement(guest_os, "kernel").text = str(work / "vmlinuz")
    ET.SubElement(guest_os, "initrd").text = str(work / "initrd.img")
    ET.SubElement(guest_os, "cmdline").text = (
        f"inst.stage2=hd:LABEL={ISO_LABEL} inst.ks=hd:LABEL=FOCUSVMKS:/focusvm.ks "
        "inst.cmdline inst.ksstrict inst.notmux inst.nosave=all console=ttyS0,115200n8"
    )
    features = ET.SubElement(domain, "features")
    ET.SubElement(features, "acpi")
    ET.SubElement(features, "apic")
    ET.SubElement(domain, "cpu", {"mode": "host-model"})
    ET.SubElement(domain, "on_reboot").text = "destroy"
    devices = ET.SubElement(domain, "devices")
    for path, target, disk_type, bus, disk_format in (
        (work / "install.qcow2", "vda", "disk", "virtio", "qcow2"),
        (iso, "sda", "cdrom", "sata", "raw"),
        (work / "kickstart.iso", "sdb", "cdrom", "sata", "raw"),
    ):
        disk = ET.SubElement(devices, "disk", {"type": "file", "device": disk_type})
        ET.SubElement(disk, "driver", {"name": "qemu", "type": disk_format})
        ET.SubElement(disk, "source", {"file": str(path)})
        ET.SubElement(disk, "target", {"dev": target, "bus": bus})
        if disk_type == "cdrom":
            ET.SubElement(disk, "readonly")
    network = ET.SubElement(devices, "interface", {"type": "user"})
    ET.SubElement(network, "model", {"type": "virtio"})
    ET.SubElement(network, "backend", {"type": "passt"})
    serial = ET.SubElement(devices, "serial", {"type": "file"})
    ET.SubElement(
        serial, "source", {"path": str(work / "install.log"), "append": "off"}
    )
    ET.SubElement(serial, "target", {"port": "0"})
    return ET.tostring(domain, encoding="unicode")


def build(args: Arguments) -> None:
    parsed = urlsplit(args.tree)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.query
        or parsed.fragment
        or any(c.isspace() for c in args.tree)
        or not args.tree.endswith("/")
    ):
        raise BuildError(
            "The install tree must be an HTTPS directory URL ending with /."
        )
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", args.version):
        raise BuildError(
            "Provide a build version of 1–80 letters, digits, dots, underscores, or dashes"
        )
    if not 1 <= args.timeout <= 240:
        raise BuildError("The install timeout must be between 1 and 240 minutes")
    if args.sign_with and not re.fullmatch(r"[a-fA-F0-9]{40,64}", args.sign_with):
        raise BuildError(
            "Pass the signing key's full hexadecimal fingerprint to --sign-with"
        )
    if sys.platform != "linux" or os.uname().machine != "x86_64" or os.geteuid() == 0:
        raise BuildError(
            "Run the builder as a regular user on an x86-64 Linux KVM host"
        )

    try:
        driver = cast("Libvirt", importlib.import_module("libvirt"))
    except ImportError as error:
        raise BuildError(
            "Install libvirt's Python binding for the build interpreter"
        ) from error
    output = args.output.resolve()
    output.mkdir(mode=0o700, parents=True)
    work = output / "work"
    work.mkdir(mode=0o700)
    print(f"Build directory: {output}", flush=True)

    # QEMU opens a private copy so it sees the same ISO bytes that passed verification.
    iso = work / ISO_NAME
    shutil.copyfile(args.iso, iso)
    verify_installer(iso, args.checksum.resolve(), args.fedora_keyring.resolve(), work)
    template = (HERE / "fedora-44.ks").read_text(encoding="utf-8")
    if template.splitlines().count(f"url --url={FEDORA_TREE}") != 1:
        raise BuildError("Kickstart must contain the expected Fedora 44 install source")
    kickstart = work / "focusvm.ks"
    kickstart.write_text(
        template.replace(f"url --url={FEDORA_TREE}", f"url --url={args.tree}"),
        encoding="utf-8",
    )
    run(
        "xorriso",
        "-as",
        "mkisofs",
        "-quiet",
        "-V",
        "FOCUSVMKS",
        "-R",
        "-o",
        work / "kickstart.iso",
        kickstart,
    )
    run(
        "xorriso",
        "-osirrox",
        "on",
        "-indev",
        iso,
        "-extract",
        "/images/pxeboot/vmlinuz",
        work / "vmlinuz",
        "-extract",
        "/images/pxeboot/initrd.img",
        work / "initrd.img",
    )
    run("qemu-img", "create", "-f", "qcow2", work / "install.qcow2", "12G")
    try:
        connection = driver.open("qemu:///session")
        if connection is None:
            raise BuildError(
                "Cannot connect to qemu:///session for the disposable image build"
            )
        try:
            install_log = work / "install.log"
            print(
                f"Installing Fedora. Follow progress with tail -f {install_log}",
                flush=True,
            )
            run_guest(
                connection,
                installer_xml(iso, work),
                install_log,
                "FOCUSVM_INSTALL_COMPLETE",
                "FOCUSVM_INSTALL_FAILED",
                args.timeout * 60,
            )
            image = work / "fedora-44.qcow2"
            run(
                "qemu-img",
                "convert",
                "-f",
                "qcow2",
                "-O",
                "qcow2",
                work / "install.qcow2",
                image,
            )
            finalize(connection, image, output, args.version, args.tree, args.sign_with)
        finally:
            connection.close()
    except driver.libvirtError as error:
        raise BuildError(f"Libvirt image build failed: {error}") from error


def finalize(
    connection: SmokeConnection,
    image: Path,
    output: Path,
    version: str,
    tree: str,
    sign_with: str | None,
) -> None:
    work = output / "work"
    install_log = work / "install.log"
    run("qemu-img", "check", "-q", "-f", "qcow2", image)
    digest = sha256(image)
    print(
        f"Booting a disposable offline overlay. Verification log: {work / 'verify.log'}",
        flush=True,
    )
    verify_guest(connection, image, work, 600)
    if sha256(image) != digest:
        raise BuildError(
            "The guest verification unexpectedly modified its backing image"
        )

    manifest = work / "guest.json"
    manifest.write_text(
        json.dumps(
            {
                "manifest_version": 1,
                "version": version,
                "architecture": "x86_64",
                "format": "qcow2",
                "firmware": "bios",
                "sha256": digest,
                "virtual_size_bytes": 12 * GIB,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    signature = work / "guest.sig"
    if sign_with:
        # Leave GnuPG connected to the terminal/agent for an interactive signing key.
        gpg = shutil.which("gpg")
        if gpg is None:
            raise BuildError("gpg is required to sign the guest manifest")
        subprocess.run(  # noqa: S603 - argv is passed directly to the installed signer.
            [
                gpg,
                "--local-user",
                sign_with,
                "--output",
                str(signature),
                "--detach-sign",
                str(manifest),
            ],
            check=True,
            timeout=120,
        )

    packages = sorted(
        line.partition(" ")[2]
        for line in install_log.read_text(
            encoding="utf-8", errors="replace"
        ).splitlines()
        if line.startswith("FOCUSVM_RPM ")
    )
    if not packages:
        raise BuildError("The installer did not report an installed package inventory")
    (output / "packages.txt").write_text("\n".join(packages) + "\n", encoding="utf-8")
    (output / "build-inputs.json").write_text(
        json.dumps(
            {
                "installer": ISO_NAME,
                "installer_sha256": ISO_SHA256,
                "fedora_signing_key": FEDORA_KEY,
                "repository": tree,
                "kickstart_sha256": sha256(work / "focusvm.ks"),
                "verification_script_sha256": sha256(HERE / "verify.sh"),
                "qemu_img": run("qemu-img", "--version").splitlines()[0],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    image.rename(output / "guest.qcow2")
    manifest.rename(output / "guest.json")
    if sign_with:
        signature.rename(output / "guest.sig")
    print(f"Verified guest artifacts: {output}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--iso", type=Path, required=True, help="Pinned Fedora 44 Server netinstall ISO"
    )
    parser.add_argument(
        "--checksum",
        type=Path,
        required=True,
        help="Signed Fedora Server checksum file",
    )
    parser.add_argument(
        "--fedora-keyring",
        type=Path,
        required=True,
        help="Binary keyring with the Fedora 44 release key",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="A new directory for the image, manifest, and build logs",
    )
    parser.add_argument(
        "--version", required=True, help="Version to place in the guest manifest"
    )
    parser.add_argument(
        "--tree",
        default=FEDORA_TREE,
        help="HTTPS mirror of the pinned Fedora 44 release tree",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=45,
        help="Installer timeout in minutes (default: 45)",
    )
    parser.add_argument(
        "--sign-with",
        help="Optional OpenPGP signing key fingerprint for the guest manifest",
    )
    args = parser.parse_args(namespace=Arguments())
    try:
        build(args)
    except (
        BuildError,
        SmokeError,
        ImportError,
        OSError,
        ValueError,
        ProvisioningError,
        ProvisioningUnavailable,
        subprocess.SubprocessError,
    ) as error:
        print(f"FocusVM guest build: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(
            "FocusVM guest build interrupted. See the build directory for logs.",
            file=sys.stderr,
        )
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
