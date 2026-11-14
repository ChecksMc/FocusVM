# Changelog

## Unreleased

- Install FocusVM for the current user with `./install.sh`. The default installation adds a desktop menu entry. Pass `--cli-only` for a terminal installation.
- Prepare a Fedora host with `./setup-fedora.sh`. Run `focusvm setup` or use the first-run wizard to detect a usable storage pool and save settings.
- Remove the user-installed application with `focusvm uninstall` while keeping VMs, resources, and settings.
- Verify a signed guest image before import with `focusvm verify-image`, export a stopped VM's standalone disk with `focusvm backup`, and share prerequisite check results with `focusvm doctor --redact --json`.
- Check clean wheel and source installs in CI for Python 3.12 and 3.14. [Preview compatibility](docs/compatibility.md)

## 0.1.0rc1

First release candidate of FocusVM, an offline Linux development environment for independent programming.

### Included

- Linux CLI and optional PySide6 desktop manager with shared configuration, host diagnostics, and managed QEMU/KVM VM creation and lifecycle controls.
- Strict and Balanced sessions both boot the guest without a network interface. The host viewer supports fullscreen and recovery after closing the window. On Wayland, Cage is an optional viewer mode.
- Deep Work, Research, and Contest profiles. Contest supports an optional countdown. Research can use references saved on the host from exact approved public HTTPS hosts.
- Read-only reference discs for local PDFs, HTML, Markdown, ZIM archives, and selected offline Python wheels. The separate Fedora 44.2 guest build includes a C++ toolchain, Python, Git, Atril, Firefox, Kiwix tools, and local C++ references.
- Offline Git bundles, explicit host-side Git fetch and push, a local session journal with project totals, and an opt-in WakaTime queue for manually recorded activity.

### Compatibility and verification

- The core VM and viewer recovery flow passed on Fedora 44 x86-64 with KDE Plasma 6.8.2 on Wayland. The same flow passed under GNOME Shell 50.6 on an isolated virtual Wayland monitor. Full Fedora Workstation login and physical shortcut checks are pending. [Verification record](docs/verification.md#phase-8-development-checks)
- The host package requires Python 3.12 or newer. VM operation requires working KVM, the system libvirt connection, and local SPICE support. Other Linux desktops are compatibility candidates. [Installation requirements](docs/setup.md#reference-hosts)

### Limitations

- Flexible guest networking and custom profile editing are planned. Git remote operations and WakaTime uploads run on the host outside focus sessions.
- Live WakaTime upload and guest bundle extraction with `guestfish` have not been verified. WakaTime recording remains manual.
- The Fedora guest is versioned separately. The available Fedora 44.2 development image is unsigned. Use the guest build guide for local provisioning with a trusted signing key. [Guest build and provisioning](docs/guest.md)
- Hosted CI for the candidate commit and publication remain pending. [Release procedure](docs/release.md)
