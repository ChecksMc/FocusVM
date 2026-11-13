"""Command for an attached, local virt-viewer console."""

import shutil
from uuid import UUID

from .virtualization import URI, VirtualizationUnavailable


def console_command(vm: UUID) -> tuple[str, list[str]]:
    executable = shutil.which("virt-viewer")
    if executable is None:
        raise VirtualizationUnavailable("Install virt-viewer to open the VM console")
    return executable, ["--connect", URI, "--attach", "--uuid", str(vm)]
