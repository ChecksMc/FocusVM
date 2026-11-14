# Preview compatibility

The current preview targets x86-64 Linux with KVM, system libvirt, and SPICE. The available packages contain the host application. A guest image is a separate installation input.

| Environment | Evidence |
| --- | --- |
| Fedora 44 KDE Plasma on Wayland | Native VM creation and startup, fullscreen viewer and recovery, and graceful stop with the Fedora 44.2 development image. |
| Fedora 44 GNOME Shell on a virtual Wayland monitor | Native viewer and recovery checks with a disposable KVM image. Full Fedora Workstation login, physical shortcuts, and acceptance of the Fedora 44.2 guest remain pending. |
| Other Fedora releases, Linux distributions, or desktop sessions | Compatibility candidates when the same libvirt and viewer capabilities are available. |
| Python 3.12 and 3.14 | CI targets the application minimum and the interpreter used on the reference Fedora 44 host. The distro libvirt binding must load in the Python that runs FocusVM. |
| Fedora 44.2 guest recipe | Separately versioned build pinned to a Fedora installer. A trusted publisher-signed image is currently unavailable. |

The reference follows Fedora's upstream support lifecycle. Before moving to a new Fedora version, rerun host acceptance on both desktop sessions and rebuild and check the guest recipe against that version. Report preview issues against current `main` or the latest candidate. [Fedora release lifecycle](https://docs.fedoraproject.org/en-US/releases/lifecycle/) · [Verification record](verification.md) · [Security policy](../SECURITY.md)

## Preview limits

| Area | Current behavior |
| --- | --- |
| Isolation | Strict and Balanced guests start without a NIC. Host administrators and other authorized libvirt clients can change devices after the checkpoints. |
| Distribution | Install from source or a Python artifact. Build and sign a guest locally, or use your own trusted publisher. |
| VM maintenance | The CLI exports standalone disk copies of stopped guests with an authorized readable disk chain. In-place restore and managed VM deletion are reserved for a later workflow. |
| Networking and profiles | Flexible guest networking and custom profile editing are unavailable. Research retrieval and remote synchronization are explicit host operations. |
| Optional integrations | Live WakaTime upload and `guestfish` extraction have limited verification. |

## CLI and configuration compatibility

Use `--json` on commands that offer it. Scripts should use named fields and tolerate added fields. Parse the exit status before consuming the result. Exit `0` means success, `2` invalid input or usage, `3` unavailable prerequisites or capability, `4` operation failure, and `130` interruption. A diagnostic JSON report can contain failed checks and return `3`.

The current configuration schema is version 1. A newer unsupported schema is rejected. Back up settings and the VM inventory before upgrading. CLI and schema changes that require user action belong in the changelog. Until the first stable line, pin the version in scripts that need an exact result format. [Settings architecture](architecture.md#settings-runtime-state-and-storage)
