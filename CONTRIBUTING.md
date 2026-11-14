# Contributing to FocusVM

## Development setup

Use Linux and Python 3.12 or newer. Follow the host setup for KVM, libvirt, QEMU, and virt-viewer if your change needs a running guest. On Fedora, use the system Python and expose its installed packages to the virtual environment so FocusVM can load the distribution's libvirt binding. Run FocusVM as your regular desktop user. [Host setup](docs/setup.md#reference-hosts)

From the repository root, create a development environment and install the declared development and desktop dependencies.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev,gui]'
```

For native VM work, create the environment with `--system-site-packages`. Install `xorriso`, Git, and the image verification tools to exercise their local integration tests. The guest recipe and acceptance scripts live under `guest/`. [Development workflow](docs/setup.md#development-workflow) · [Guest build and provisioning](docs/guest.md)

## Code and desktop changes

Keep shared behavior in the Python services under `src/focusvm/`. The CLI and Qt manager use those services for configuration, VM operations, and focus sessions. Keep PySide6 imports in the GUI package and keep blocking libvirt and file operations off the GUI thread. For changes to a window or dialog, check that keyboard navigation and error messages still work, and open the manager in a local desktop session. [Architecture](docs/architecture.md)

Preserve the behavior of existing command and configuration formats. Use Ruff for formatting and lint, and keep Mypy and Pyright passing in strict mode. Give errors enough context to identify the failed operation and a relevant next step. Add regression coverage when changing validation, persistence, or recovery.

## Checks

```sh
ruff format --check .
ruff check .
python -m pyright --pythonpath "$(command -v python)"
mypy
QT_QPA_PLATFORM=offscreen python -m pytest --cov=focusvm --cov-report=term-missing
python -m build
pip-audit --skip-editable
```

The offscreen tests exercise Qt widgets and shared services. For changes involving networking, VM startup, storage, or fullscreen behavior, use a disposable VM and check the actual libvirt and viewer behavior on a KVM-capable host. Fedora KDE Plasma and Fedora Workstation with GNOME on Wayland are the reference desktops. Record the host and guest versions, the checks performed, and any skipped checks. [Verification and release](docs/verification.md)

## Commits and pull requests

Use focused Conventional Commits such as `fix(vm): handle interrupted volume uploads` and `docs: clarify Fedora setup`. Open a pull request with the reason for the change, any user-visible behavior, and the checks you ran. For GUI changes, include a screenshot of the affected view when it helps review. Keep credentials, VM images, build outputs, and user-specific settings out of commits and screenshots. Route security reports through the process below. [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/) · [Security policy](SECURITY.md)
