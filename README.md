# FocusVM

FocusVM is a Linux project for practicing independent programming. Its development VM is designed to keep a terminal, tools, and local references close at hand so that reaching for AI assistance or switching tasks takes a deliberate step. You can leave a focus session at any time.

The PySide6 manager and CLI share configuration and libvirt services. They can run an offline Fedora guest with Vim, Git, C++, Python, and debugging tools, and open it in a fullscreen focus session. Fedora KDE Plasma and Fedora Workstation with GNOME, both on Wayland, are the first verification targets. Other Linux distributions may work with the same dependencies and permissions.

## Install and check the host

Use Python 3.12 or newer. Install the [host prerequisites](docs/setup.md#reference-hosts), then use the system interpreter so FocusVM can load the distribution's libvirt binding.

```sh
python3 -m venv --system-site-packages .venv-vm
. .venv-vm/bin/activate
python -m pip install -e .
focusvm init --storage-pool focusvm
focusvm config validate
focusvm doctor
```

`init` records the pool name in your XDG configuration. With KVM, libvirt, and the host tools installed, the CLI can verify a signed local guest image and create, configure, start, stop, and inspect offline VMs. See the [CLI setup](docs/setup.md#phase-2-cli-workflow) for the required image and host tools.

## Desktop manager

Install the optional Qt dependencies and launch the manager from your Linux desktop session.

```sh
python -m pip install -e '.[gui]'
focusvm gui
```

The manager provides a first-run wizard, host checks, VM management, and shared settings. The [Fedora guest build guide](docs/guest.md#build-the-base-image) shows how to build the offline development image. [Desktop setup and usage](docs/setup.md#desktop-manager)

## Start a focus session

Select your VM in the desktop manager, then open **Focus Sessions** and choose **Start Focus Session**. From a host terminal you can also run the following commands.

```sh
focusvm session start --profile deep-work
focusvm session status
focusvm session end
```

**Shift+F12** releases input and **Shift+F11** toggles fullscreen. Ending the session closes its viewer and leaves the offline guest running. [Focus sessions and recovery](docs/sessions.md#start-end-and-recover)

## Offline references and synchronization

Import PDF, HTML, Markdown, and ZIM files in the manager’s **Resources** page. The CLI can attach a read-only reference disc, transfer Git bundles, and queue opt-in WakaTime events. [Offline resources and synchronization](docs/resources-and-sync.md)

## Design and setup

- [Architecture and configuration](docs/architecture.md) covers the package structure, service boundaries, state ownership, and CLI.
- [Host and development setup](docs/setup.md) covers prerequisites, diagnostics, and the proposed first run.
- [Guest image and provisioning](docs/guest.md) covers the build recipe, distribution, and per-VM storage.
- [Focus sessions and networking](docs/sessions.md) covers offline profiles, fullscreen, and session recovery.
- [Verification and release](docs/verification.md) covers implementation order and the tests required to claim support.

This project is partially AI-generated.
