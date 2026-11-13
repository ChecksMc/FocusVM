# FocusVM

FocusVM is a Linux project for practicing independent programming. Its development VM is designed to keep a terminal, tools, and local references close at hand so that reaching for AI assistance or switching tasks takes a deliberate step. You can leave a focus session at any time.

The first usable milestone will pair a PySide6 desktop manager with a CLI that uses the same configuration and libvirt services. It will create and run an offline Fedora guest with Vim, Git, C++, Python, and debugging tools, and open it in a fullscreen viewer. Fedora KDE Plasma and Fedora Workstation with GNOME, both on Wayland, are the first verification targets. Other Linux distributions may work with the same dependencies and permissions.

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

The manager provides a first-run wizard, host checks, VM management, and shared settings. See [Desktop setup and usage](docs/setup.md#desktop-manager). The FocusVM guest image and fullscreen focus sessions are upcoming milestones.

## Design and setup

- [Architecture and configuration](docs/architecture.md) covers the package structure, service boundaries, state ownership, and CLI.
- [Host and development setup](docs/setup.md) covers prerequisites, diagnostics, and the proposed first run.
- [Guest image and provisioning](docs/guest.md) covers the build recipe, distribution, and per-VM storage.
- [Focus sessions and networking](docs/sessions.md) covers offline profiles, fullscreen, and session recovery.
- [Verification and release](docs/verification.md) covers implementation order and the tests required to claim support.

This project is partially AI-generated.
