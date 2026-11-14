# FocusVM

FocusVM is a source-first technical preview for Linux developers who want to practice programming with local tools and references. It is aimed at users comfortable setting up KVM and libvirt. The host application is available separately from the guest. **There is no official signed guest download or publisher key yet.** Build and sign your own image or bring one from a publisher you trust. [Guest image guide](docs/guest.md)

The PySide6 manager and CLI share settings and manage the same QEMU/KVM VMs through libvirt. The Fedora guest includes Vim, Git, C++, Python, debugging tools, and offline references. Deep Work, Contest, and Research sessions can open it fullscreen.

## Host compatibility

Use an x86-64 Linux host with Python 3.12 or newer, hardware virtualization, an authorized `qemu:///system` connection, and an active directory-backed storage pool. The desktop manager uses PySide6. Fullscreen sessions use virt-viewer and local SPICE. [Host prerequisites and troubleshooting](docs/setup.md#reference-hosts)

The VM and viewer recovery flow passed on Fedora 44 KDE Plasma on Wayland and on Fedora 44 GNOME Shell using a virtual Wayland monitor. A full Fedora Workstation login and physical shortcut checks are pending. FocusVM relies on a trusted host and checks guest isolation at startup and viewer checkpoints. Host administrators retain control of the VM. [Compatibility and limitations](docs/compatibility.md) · [Security policy](SECURITY.md)

## Install and set up

On a standard Fedora installation, run these commands from the repository root. The first command asks for administrator access to install the host tools, enable local libvirt services, and prepare a dedicated `focusvm` storage pool. Skip it if your host already has the tools and an active directory-backed pool.

```sh
./setup-fedora.sh
./install.sh
focusvm setup
```

`install.sh` runs as your regular user. Open **FocusVM** from the application menu or run `focusvm gui`. Use `./install.sh --cli-only` for a terminal-only installation. If your shell cannot find the command, run `~/.local/bin/focusvm setup`.

`focusvm setup` checks the host and selects an active pool. Use `focusvm setup --storage-pool NAME` if you need to choose one. With a signed image ready, follow the [first-session walkthrough](docs/first-session.md). [Manual setup and troubleshooting](docs/setup.md)

## Desktop manager

On first launch without settings, choose **Check host** and then **Save settings**. The wizard can detect a pool when the name is blank. Use **Virtual Machines** to create, start, inspect, or shut down a guest. [Desktop setup and usage](docs/setup.md#desktop-manager)

![FocusVM Resources page with three sample local references](docs/images/resources.png)

## Uninstall

End any focus session and close the manager before running `focusvm uninstall` or `./uninstall.sh`. This removes the installed app and launchers. VMs, settings, and your resource library remain available when you reinstall. [Installation details](docs/setup.md#quick-install)

Before maintenance, use `focusvm backup` to copy a stopped VM's disk. Keep the original base image and VM inventory alongside your backups. [VM data and recovery](docs/guest.md#vm-data-and-recovery)

## Start a focus session

Select your VM in the desktop manager, then open **Focus Sessions** and choose **Start Focus Session**. From a host terminal you can also run the following commands.

```sh
focusvm session start --profile deep-work
focusvm session status
focusvm session end
```

**Shift+F12** releases input and **Shift+F11** toggles fullscreen. Ending the session closes its viewer and leaves the offline guest running. [Focus sessions and recovery](docs/sessions.md#start-end-and-recover)

Strict and Balanced guests start without a network interface. Wayland users can optionally run the viewer inside Cage. Use the manager, the Cage host window, or `focusvm session end` from another host terminal to exit Cage. [Networking and viewer behavior](docs/sessions.md) · [Optional Cage viewer](docs/advanced-sessions.md#optional-cage-viewer)

## Offline references and synchronization

Import PDF, HTML, Markdown, and ZIM files in the manager’s **Resources** page. The CLI can attach a read-only reference disc, transfer Git bundles, and explicitly fetch or push from the host outside a focus session. WakaTime records activity only when you opt in and supply events. [Offline resources and synchronization](docs/resources-and-sync.md)

Use Contest for an optional timer or Research for a session with local references. The CLI can save approved web references before a session, attach offline Python wheels, and record journal entries with optional statistics. [Advanced sessions and maintenance](docs/advanced-sessions.md)

## Development and release

- [Architecture and configuration](docs/architecture.md)
- [Development and contribution guide](CONTRIBUTING.md)
- [Security policy](SECURITY.md)
- [Release notes](CHANGELOG.md) and [release procedure](docs/release.md)
- [MIT license](LICENSE) and [third-party notices](NOTICE)

This project is partially AI-generated.
