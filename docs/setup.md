# Host and development setup

Install FocusVM from the repository. The CLI and desktop manager support managed offline VM creation from a signed local guest image and basic lifecycle management. Build the Fedora development image using the [guest build guide](guest.md#build-the-base-image).

## Reference hosts

The initial verification targets are x86-64 Fedora KDE Plasma Desktop and Fedora Workstation with GNOME, both on Wayland. The host needs hardware virtualization exposed to Linux, a working KVM device, a local libvirt QEMU connection at `qemu:///system`, Python 3.12 or newer, and enough disk space for the base image and the growing VM disk. The GUI uses PySide6. The guest display uses SPICE through `virt-viewer`. [Libvirt QEMU driver](https://libvirt.org/drvqemu.html) · [Qt for Python](https://doc.qt.io/qtforpython-6/) · [Fedora virt-viewer package](https://packages.fedoraproject.org/pkgs/virt-viewer/virt-viewer/)

On a package-managed Fedora desktop, an administrator can install the host tools with DNF. Review the package transaction before accepting it. The application and all subsequent FocusVM commands run as the regular desktop user. [Fedora QEMU packages](https://packages.fedoraproject.org/pkgs/qemu/) · [Fedora libvirt packages](https://packages.fedoraproject.org/pkgs/libvirt/) · [DNF install reference](https://dnf5.readthedocs.io/en/stable/commands/install.8.html)

```sh
sudo dnf install qemu-kvm qemu-img qemu-ui-spice-core libvirt-daemon-kvm \
  libvirt-daemon-driver-storage-core libvirt-client python3-libvirt \
  virt-viewer python3 python3-pip gnupg2-verify openssl xorriso
```

Other Linux distributions can use their equivalent packages and the same libvirt URI and checks. Match the libvirt Python binding to the interpreter running FocusVM. Unknown desktop environments retain CLI VM controls. Other Wayland compositors and X11 are compatibility candidates pending testing. [Fedora libvirt Python binding](https://packages.fedoraproject.org/pkgs/libvirt-python/python3-libvirt/)

## Check the host

Run these checks as the user who will launch FocusVM, from a terminal in the graphical session when checking the desktop. The commands query host capabilities and avoid changing guest or host settings. [virt-host-validate manual](https://libvirt.org/manpages/virt-host-validate.html) · [virsh manual](https://libvirt.org/manpages/virsh.html)

```sh
uname -m
python3 --version
qemu-img --version
virt-host-validate qemu
virsh --connect qemu:///system list --all
virsh --connect qemu:///system domcapabilities --virttype kvm --arch x86_64 --machine q35
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

## Phase 2 CLI workflow

From the repository root, install the package using the same interpreter as the distribution's libvirt binding. The example uses Fedora's system Python and exposes its packages inside the virtual environment. The application runs as the authorized desktop user. [Python virtual environment guide](https://packaging.python.org/en/latest/guides/installing-using-pip-and-virtual-environments/) · [Fedora libvirt Python binding](https://packages.fedoraproject.org/pkgs/libvirt-python/python3-libvirt/)

```sh
python3 -m venv --system-site-packages .venv-vm
. .venv-vm/bin/activate
python -m pip install -e .
focusvm init --storage-pool focusvm
focusvm doctor
```

Use the actual name of your active directory pool in `init`. `doctor --json` returns structured checks. Its queries require the ordinary system libvirt connection because libvirt restricts the capability query on read-only connections. If authorization fails, check your existing host policy with the administrator. [Libvirt client authorization](https://libvirt.org/acl.html)

Supply a compatible QCOW2, its JSON manifest and detached signature, and a binary keyring containing a publisher key you trust. The [guest builder](guest.md#build-the-base-image) can produce these image files with your signing key. The repository has no published guest image or signing key yet.

```sh
focusvm create --name work --image guest.qcow2 --manifest guest.json \
  --signature guest.sig --keyring publisher.gpg
focusvm list
focusvm configure --cpus 2 --memory-mib 4096
focusvm start
focusvm status --json
focusvm stop --timeout 60
```

Creation prompts twice for the `focusvm` guest password in the terminal. It defines the VM without booting it. `create` accepts optional `--cpus`, `--memory-mib`, and `--disk-gib` overrides. `configure` changes CPU and RAM on a stopped VM. `stop` requests a graceful shutdown and reports a timeout if the VM remains active.

`start`, `status`, `stop`, and `configure` accept `--vm UUID`. Without it, the CLI uses the configured `application.default_vm` or the only non-abandoned VM in its inventory. The list shows creation phases. `status` queries libvirt for the actual VM state.

To view a running VM from the CLI, replace `UUID` with the VM's UUID. [virt-viewer manual](https://gitlab.com/virt-viewer/virt-viewer/-/raw/master/man/virt-viewer.pod)

```sh
virt-viewer --connect qemu:///system --attach --uuid UUID
```

## Desktop manager

With the host prerequisites installed, use the same system Python virtual environment as the CLI. Install the optional Qt dependencies, then run the manager from your graphical session.

```sh
python -m pip install -e '.[gui]'
focusvm gui
```

On first launch without settings, enter an active directory-backed pool in the setup wizard and run **Check host**. Save when the required checks pass. Existing CLI settings are loaded automatically. **Dashboard** shows the selected VM's status and offers host diagnostics.

In **Virtual Machines**, use **Create VM** with the [signed image inputs](guest.md#phase-2-image-manifest) and choose a guest password. Use **CPU and RAM** while the VM is stopped. Use **Start** to boot it, **Open Console** to attach a local virt-viewer window, and **Shut Down** to request guest shutdown. Use **Refresh** to load inventory changes from the CLI. The manager periodically reads the selected VM's status while displaying the dashboard or VM page.

In **Settings**, set the pool for new VMs, the default VM, creation resources, and the appearance preference. Changes are saved to the same XDG configuration as the CLI. The System appearance follows the Qt platform theme. [Qt appearance settings](https://doc.qt.io/qt-6/qstylehints.html#setColorScheme)

## Development workflow

Create a virtual environment from the repository root. Phase 1 uses Python 3.12 or newer for the configuration service and CLI. The development tools are declared in `pyproject.toml`.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev,gui]'
ruff format --check .
ruff check .
pyright
mypy
QT_QPA_PLATFORM=offscreen python -m pytest --cov=focusvm --cov-report=term-missing
python -m build
```

Pyright and Mypy run in strict mode. The pytest-qt widget tests run independently of KVM. Reserve real boot, offline guest, and fullscreen exit checks for KVM-capable Fedora hosts with both reference desktop sessions. Record the Fedora release, desktop, session type, and result for each verified environment. Use Conventional Commits for completed changes.
