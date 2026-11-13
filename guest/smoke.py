"""Transient guest lifetime and offline image acceptance."""

import importlib
import json
import shutil
import subprocess
from abc import ABC, abstractmethod
from pathlib import Path
from time import monotonic, sleep
from typing import Protocol, cast
from uuid import uuid4
from xml.etree import ElementTree as ET

_AUTODESTROY = 2  # VIR_DOMAIN_START_AUTODESTROY
_LOG_TAIL_BYTES = 8192


class SmokeError(RuntimeError):
    """A disposable guest did not complete its build or verification."""


class _Domain(Protocol):
    def isActive(self) -> int: ...
    def destroy(self) -> int: ...


class SmokeConnection(Protocol):
    def createXML(self, xml: str, flags: int, /) -> _Domain | None: ...


class _LibvirtError(Exception, ABC):
    @abstractmethod
    def get_error_code(self) -> int: ...


class _Libvirt(Protocol):
    libvirtError: type[_LibvirtError]
    VIR_ERR_NO_DOMAIN: int


def run_guest(
    connection: SmokeConnection,
    xml: str,
    log: Path,
    success: str,
    failure: str,
    timeout: int,
) -> None:
    """Run one transient domain until it reports success and powers off."""
    log.touch(mode=0o600, exist_ok=False)
    domain = connection.createXML(xml, _AUTODESTROY)
    if domain is None:
        raise SmokeError(f"Libvirt could not start the disposable guest. Log: {log}")
    driver = cast("_Libvirt", importlib.import_module("libvirt"))
    deadline = monotonic() + timeout
    active = True

    def is_active() -> bool:
        try:
            return bool(domain.isActive())
        except driver.libvirtError as error:
            if error.get_error_code() == driver.VIR_ERR_NO_DOMAIN:
                return False
            raise

    try:
        while True:
            active = is_active()
            passed, failed = _markers(log, success, failure)
            if failed:
                raise SmokeError(_with_log("Guest reported failure", log))
            if not active:
                if passed:
                    return
                raise SmokeError(
                    _with_log("Guest stopped before reporting success", log)
                )
            if monotonic() >= deadline:
                message = (
                    "Guest did not power off"
                    if passed
                    else "Guest did not report success"
                )
                raise SmokeError(_with_log(f"{message} within {timeout} seconds", log))
            sleep(1)
    finally:
        if active and is_active():
            try:
                domain.destroy()
            except driver.libvirtError as error:
                if error.get_error_code() != driver.VIR_ERR_NO_DOMAIN:
                    raise


def _markers(log: Path, success: str, failure: str) -> tuple[bool, bool]:
    with log.open(encoding="utf-8", errors="replace") as stream:
        lines = {line.strip() for line in stream if line.endswith("\n")}
    return success in lines, failure in lines


def _with_log(message: str, log: Path) -> str:
    with log.open("rb") as stream:
        stream.seek(max(0, log.stat().st_size - _LOG_TAIL_BYTES))
        tail = stream.read().decode("utf-8", errors="replace").strip()
    return f"{message}. Log: {log}\n{tail}"


def verify_guest(
    connection: SmokeConnection, image: Path, work: Path, timeout: int
) -> None:
    from focusvm.provisioning import build_seed

    instance = uuid4()
    overlay = work / "verification-overlay.qcow2"
    seed = work / "verification-seed.iso"
    log = work / "verify.log"
    verifier = Path(__file__).with_name("verify.sh").read_text(encoding="utf-8")
    _command(
        "qemu-img", "create", "-f", "qcow2", "-F", "qcow2", "-b", image, overlay, "14G"
    )

    seed_dir = work / "verification-seed"
    seed_dir.mkdir(mode=0o700)
    userdata = seed_dir / "user-data"
    with build_seed(instance, uuid4().hex) as original_seed:
        _command(
            "xorriso",
            "-osirrox",
            "on",
            "-indev",
            original_seed,
            "-extract",
            "/user-data",
            userdata,
        )
    metadata = seed_dir / "meta-data"
    metadata.write_text(f"instance-id: {instance}\n", encoding="utf-8")
    command = (
        "systemd-run --unit=focusvm-verify --property=After=cloud-final.service "
        "--no-block /usr/bin/bash /var/tmp/focusvm-verify-runner.sh"
    )
    runner = (
        "#!/usr/bin/bash\n"
        "set -u\n"
        "report=$(mktemp)\n"
        'if runuser -u focusvm -- bash /var/tmp/focusvm-verify.sh --serial >"$report" 2>&1; then\n'
        '  cat "$report" > /dev/ttyS0\n'
        "else\n"
        "  { cat \"$report\"; printf '\\nFOCUSVM_VERIFY_FAILED\\n'; } > /dev/ttyS0\n"
        "fi\n"
        'rm -f -- "$report"\n'
        "systemctl poweroff --no-block\n"
    )
    with userdata.open("a", encoding="utf-8") as stream:
        stream.write(
            "\nwrite_files:\n  - path: /var/tmp/focusvm-verify.sh\n"
            f"    permissions: '0755'\n    content: {json.dumps(verifier, ensure_ascii=False)}\n"
            "  - path: /var/tmp/focusvm-verify-runner.sh\n"
            f"    permissions: '0700'\n    content: {json.dumps(runner)}\n"
            f"runcmd:\n  - {json.dumps(['bash', '-c', command])}\n"
        )
    _command(
        "xorriso",
        "-as",
        "mkisofs",
        "-quiet",
        "-V",
        "cidata",
        "-R",
        "-o",
        seed,
        metadata,
        userdata,
    )
    seed.chmod(0o600)

    root = ET.Element("domain", {"type": "kvm"})
    ET.SubElement(root, "name").text = f"focusvm-verify-{instance.hex[:12]}"
    ET.SubElement(root, "uuid").text = str(instance)
    ET.SubElement(root, "memory", {"unit": "MiB"}).text = "4096"
    ET.SubElement(root, "vcpu").text = "2"
    os_xml = ET.SubElement(root, "os")
    ET.SubElement(os_xml, "type", {"arch": "x86_64", "machine": "q35"}).text = "hvm"
    ET.SubElement(os_xml, "boot", {"dev": "hd"})
    features = ET.SubElement(root, "features")
    ET.SubElement(features, "acpi")
    ET.SubElement(features, "apic")
    ET.SubElement(root, "on_reboot").text = "destroy"
    devices = ET.SubElement(root, "devices")
    for path, target, disk_type, bus, disk_format in (
        (overlay, "vda", "disk", "virtio", "qcow2"),
        (seed, "sda", "cdrom", "sata", "raw"),
    ):
        disk = ET.SubElement(devices, "disk", {"type": "file", "device": disk_type})
        ET.SubElement(disk, "driver", {"name": "qemu", "type": disk_format})
        ET.SubElement(disk, "source", {"file": str(path)})
        ET.SubElement(disk, "target", {"dev": target, "bus": bus})
        if disk_type == "cdrom":
            ET.SubElement(disk, "readonly")
    graphics = ET.SubElement(devices, "graphics", {"type": "spice"})
    ET.SubElement(graphics, "listen", {"type": "none"})
    ET.SubElement(graphics, "clipboard", {"copypaste": "no"})
    ET.SubElement(graphics, "filetransfer", {"enable": "no"})
    ET.SubElement(ET.SubElement(devices, "video"), "model", {"type": "vga"})
    ET.SubElement(devices, "audio", {"id": "1", "type": "none"})
    serial = ET.SubElement(devices, "serial", {"type": "file"})
    ET.SubElement(serial, "source", {"path": str(log), "append": "off"})
    ET.SubElement(serial, "target", {"port": "0"})
    try:
        run_guest(
            connection,
            ET.tostring(root, encoding="unicode"),
            log,
            "FocusVM guest verification passed.",
            "FOCUSVM_VERIFY_FAILED",
            timeout,
        )
    finally:
        seed.unlink(missing_ok=True)
        shutil.rmtree(seed_dir)
    overlay.unlink()


def _command(name: str, *args: str | Path) -> None:
    program = shutil.which(name)
    if program is None:
        raise SmokeError(f"Required command is missing: {name}")
    result = subprocess.run(  # noqa: S603 - argv goes directly to an installed tool.
        [program, *map(str, args)],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if result.returncode:
        raise SmokeError(f"{name} failed: {result.stderr.strip()}")
