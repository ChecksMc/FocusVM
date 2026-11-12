# FocusVM

FocusVM is a planned Linux development environment for practicing independent programming. It puts a terminal, development tools, and local references in a VM so that reaching for AI assistance or switching to another task takes a deliberate step. You can leave a focus session at any time.

The first usable milestone will pair a PySide6 desktop manager with a CLI that uses the same configuration and libvirt services. It will create and run an offline Fedora guest with Vim, Git, C++, Python, and debugging tools, and open it in a fullscreen viewer. Fedora KDE Plasma and Fedora Workstation with GNOME, both on Wayland, are the first verification targets. Other Linux distributions may work with the same dependencies and permissions.

## Design and setup

This repository is in the design phase. The setup guide includes host checks you can run now and describes the proposed installation and first-run workflow.

- [Architecture and configuration](docs/architecture.md) covers the package structure, service boundaries, state ownership, and CLI.
- [Host and development setup](docs/setup.md) covers prerequisites, diagnostics, and the proposed first run.
- [Guest image and provisioning](docs/guest.md) covers the build recipe, distribution, and per-VM storage.
- [Focus sessions and networking](docs/sessions.md) covers offline profiles, fullscreen, and session recovery.
- [Verification and release](docs/verification.md) covers implementation order and the tests required to claim support.

This project is partially AI-generated.
