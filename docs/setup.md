# Host and development setup

The Phase 1 CLI is installable from the repository and supports initializing and validating settings. The virtualization host checks use existing Linux tools. The full first-run workflow below includes planned VM and desktop features.

## Reference hosts

The initial verification targets are x86-64 Fedora KDE Plasma Desktop and Fedora Workstation with GNOME, both on Wayland. The host needs hardware virtualization exposed to Linux, a working KVM device, a local libvirt QEMU connection at `qemu:///system`, Python 3.12 or newer, and enough disk space for the base image and the growing VM disk. The GUI uses PySide6. The guest display uses SPICE through `virt-viewer`. [Libvirt QEMU driver](https://libvirt.org/drvqemu.html) · [Qt for Python](https://doc.qt.io/qtforpython-6/) · [Fedora virt-viewer package](https://packages.fedoraproject.org/pkgs/virt-viewer/virt-viewer/)

On a package-managed Fedora desktop, an administrator can install the host tools with DNF. Review the package transaction before accepting it. The application and all subsequent FocusVM commands run as the regular desktop user. [Fedora QEMU packages](https://packages.fedoraproject.org/pkgs/qemu/) · [Fedora libvirt packages](https://packages.fedoraproject.org/pkgs/libvirt/) · [DNF install reference](https://dnf5.readthedocs.io/en/stable/commands/install.8.html)

```sh
sudo dnf install qemu-kvm qemu-img qemu-ui-spice-core libvirt-daemon-qemu \
  libvirt-daemon-driver-storage-core libvirt-client virt-viewer python3 python3-pip
```

Other Linux distributions can use their equivalent packages and the same libvirt URI and checks. Match the libvirt Python binding to the Python interpreter used by FocusVM. Unknown desktop environments should retain VM controls and report unavailable desktop features. Other Wayland compositors and X11 are compatibility candidates pending testing.

## Check the host

Run these checks as the user who will launch FocusVM, from a terminal in the graphical session when checking the desktop. The commands query host capabilities and avoid changing guest or host settings. [virt-host-validate manual](https://libvirt.org/manpages/virt-host-validate.html) · [virsh manual](https://libvirt.org/manpages/virsh.html)

```sh
uname -m
python3 --version
qemu-img --version
virt-host-validate qemu
virsh --connect qemu:///system list --all
virsh --connect qemu:///system domcapabilities --virttype kvm --arch x86_64
virsh --connect qemu:///system pool-list --all --details
virt-viewer --version
printf 'Session type: %s\nDesktop: %s\n' \
  "${XDG_SESSION_TYPE:-unknown}" "${XDG_CURRENT_DESKTOP:-unknown}"
```

| Symptom | What to check |
| --- | --- |
| KVM is missing or libvirt cannot report KVM capabilities | Check hardware virtualization in the host firmware and whether Linux exposes `/dev/kvm`. On a virtualized host, the outer hypervisor also needs to expose nested virtualization. |
| The libvirt socket is missing | On a systemd host, inspect `systemctl status virtqemud.socket virtstoraged.socket` for modular libvirt or `systemctl status libvirtd.socket libvirtd.service` for monolithic libvirt. Ask the administrator to enable the appropriate local services for the existing layout. |
| The connection or a specific operation is denied | Confirm the login's libvirt authorization with the host administrator. Listing VMs or pools can succeed with read access, while creating and running VMs needs additional rights. Keep authorization limited to the intended users and host policy. |
| The viewer is installed but the graphics capabilities lack `spice` | Check that the host QEMU build includes SPICE server support. The capability must be present under the `graphics` device for the selected domain type. |
| The pool is inactive, out of space, or inaccessible to QEMU | Confirm the pool status and free space in libvirt. The QEMU process needs access to each disk and its backing image, including directory traversal and applicable SELinux labels. |

See the [libvirt daemon guide](https://libvirt.org/daemons.html), [client authorization guide](https://libvirt.org/acl.html), [QEMU storage permissions](https://libvirt.org/drvqemu.html#posix-users-groups), and [domain capability format](https://libvirt.org/formatdomaincaps.html#graphical-framebuffers).

## Prepare a storage pool

Use an active directory-backed libvirt storage pool that the desktop user is authorized to use. The first-run wizard will list suitable pools and let the user pick one. The pool holds the verified read-only guest base and each VM's writable QCOW2 overlay. Leave room for the overlay to grow as files are written in the guest. [Libvirt storage management](https://libvirt.org/storage.html#directory-pool) · [QEMU disk images](https://www.qemu.org/docs/master/system/images.html)

If an administrator wants a dedicated pool on Fedora, this example defines a new pool under Fedora's libvirt image directory. Use it only if the `focusvm` pool name and target directory are both unused. The target path is an example for Fedora. Pool selection in FocusVM is by libvirt identifier. [Virsh storage pool commands](https://libvirt.org/manpages/virsh.html#storage-pool-commands)

```sh
sudo virsh --connect qemu:///system pool-define-as focusvm dir \
  --target /var/lib/libvirt/images/focusvm
sudo virsh --connect qemu:///system pool-build focusvm
sudo virsh --connect qemu:///system pool-start focusvm
sudo virsh --connect qemu:///system pool-autostart focusvm
```

## Planned first run

1. Install the FocusVM Python package into an isolated environment from a published wheel. Keep QEMU, libvirt, and the viewer under the host package manager. The install command will be finalized when the packaging and release artifacts exist. [Python virtual environment guide](https://packaging.python.org/en/latest/guides/installing-using-pip-and-virtual-environments/)
2. Launch the Qt manager with `focusvm gui` and open the first-run wizard. The CLI equivalent begins with `focusvm doctor` and `focusvm init`. Report each dependency failure with the check that failed and a way to resolve it.
3. Select the libvirt storage pool and import the FocusVM Fedora image. The application verifies the signature on the FocusVM manifest and matches the image digest before using it. See the [guest build recipe and image format](guest.md#build-the-base-image).
4. Choose VM CPU, RAM, and disk limits, then create the VM through the wizard or the proposed `focusvm create` command. FocusVM creates a writable overlay and supplies a local cloud-init NoCloud seed for a unique guest login on first boot. [cloud-init NoCloud data source](https://docs.cloud-init.io/en/latest/reference/datasources/nocloud.html)
5. Select **Deep Work** in the manager or run `focusvm session start --profile deep-work`. Confirm that the fullscreen guest opens, then try Vim, Git, Python, or the C++ toolchain in the guest terminal.
6. Save work in the guest and end the focus session through the manager or `focusvm session end`. Use the VM stop action when you want to shut down the guest. The session behavior and recovery plan are described in [Focus sessions](sessions.md).

## Development workflow

Create a virtual environment from the repository root. Phase 1 uses Python 3.12 or newer for the configuration service and CLI. The development tools are declared in `pyproject.toml`.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
ruff format --check .
ruff check .
pyright
mypy
python -m pytest --cov=focusvm --cov-report=term-missing
python -m build
```

Pyright and Mypy run in strict mode. Add pytest-qt with the Qt manager and run the widget tests independently of KVM. Reserve real boot, offline guest, and fullscreen exit checks for KVM-capable Fedora hosts with both reference desktop sessions. Record the Fedora release, desktop, session type, and result for each verified environment. Use Conventional Commits for completed changes.
