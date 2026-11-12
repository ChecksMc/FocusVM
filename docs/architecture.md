# Architecture

FocusVM uses one local QEMU/KVM backend through libvirt. The desktop manager and CLI run as the signed-in user and call the same Python services. The initial reference host is x86_64 Linux with KVM. The first compatibility runs target Fedora KDE Plasma and Fedora Workstation GNOME on Wayland. The guest has its own desktop and does not inherit the host desktop or package manager. [libvirt connection URIs](https://libvirt.org/uri.html)

## Package structure

Add modules when their first feature lands. The initial Python package uses a `src` layout and `pyproject.toml` for packaging and development tooling.

```text
src/focusvm/
  config.py          Settings, profile validation, and TOML migrations
  cli.py             Command parsing, text and JSON output, and exit codes
  diagnostics.py     Host and dependency checks
  virtualization.py  Libvirt connection, domain definitions, and storage
  provisioning.py    Image verification and per-VM preparation
  sessions.py        Session transitions, offline policy, and recovery
  viewer.py          Local virt-viewer launch and exit handling
  gui/               PySide6 manager and first-run wizard
guest/               Guest build recipe and image manifest tooling
tests/               Unit, GUI, and KVM integration suites
```

Both entry points call the configuration, diagnostics, virtualization, provisioning, and session services directly. Sessions coordinate the VM and viewer services. The viewer module owns subprocess launch and release behavior. Keep the core free of Qt imports and give the Qt manager worker threads for blocking libvirt, image, and file operations. Workers return results through Qt signals and the GUI refreshes against shared state. Show controls only for implemented actions. Begin with the dashboard, VM management, sessions, and essential settings, then add resource and integration pages with their corresponding services.

Use the native `libvirt-python` bindings for management. Open `qemu:///system` explicitly and require KVM for the reference release. `virsh` is useful for host diagnostics and recovery. Require a usable system libvirt connection and a configured directory-backed storage pool. Libvirt and the host's access controls govern permission checks. The application checks for missing privileges and reports the failing operation to the user. [QEMU/KVM driver](https://libvirt.org/drvqemu.html) · [Libvirt storage pools and volumes](https://libvirt.org/storage.html)

The same Qt widgets and viewer launcher serve both Fedora desktop targets. Detect a usable graphical session and the viewer capabilities needed for fullscreen. An unknown desktop can still use the CLI and applicable GUI features. Add desktop-specific code only for capabilities that need it, such as a later dedicated kiosk session.

## Settings, runtime state, and storage

| Location | Contents |
| --- | --- |
| `$XDG_CONFIG_HOME/focusvm/config.toml` | Settings, defaults, and selected libvirt storage pool |
| `$XDG_CONFIG_HOME/focusvm/profiles/` | User-owned profile TOML files when custom profiles ship |
| `$XDG_STATE_HOME/focusvm/` | Local VM inventory, operation and session state, and logs |
| `$XDG_CACHE_HOME/focusvm/` | Replaceable downloads and staging data |
| `$XDG_DATA_HOME/focusvm/` | User resources when the resource manager ships |
| Selected libvirt pool | Verified base volumes, writable overlays, and per-VM seed media |

Use XDG defaults for unset paths. Keep settings and state files private to the user and cache files disposable. Store connection credentials for optional integrations in a supported system credential store. If one is unavailable, the integration can prompt for a credential for that operation or stay disabled. [XDG Base Directory Specification](https://specifications.freedesktop.org/basedir/latest/)

The proposed first schema keeps persistent preferences small. These values illustrate a configured workstation. `storage_pool` is chosen during setup and `default_vm` is omitted until the user selects a managed domain.

```toml
schema_version = 1

[application]
theme = "system"
default_profile = "deep-work"
storage_pool = "focusvm"

[vm_defaults]
cpus = 2
memory_mib = 4096
disk_gib = 32
```

Validate schema version, allowed values, positive resource sizes, profile IDs, and available resources at the service boundary. An unsupported future schema produces a readable error. Known migrations validate the new document before atomically replacing the old one and keep a recoverable copy. Use file replacement in the destination directory for settings and operation records. Serialize GUI and CLI mutations with a per-user lock and reload the affected state while holding it to avoid overwriting changes from another process.

Libvirt owns actual VM status, resource allocations, device configuration, and volume identity. The local inventory stores managed domain UUIDs and volume keys. Cross-check inventory against FocusVM metadata in the domain before acting. The metadata identifies app-managed resources and libvirt still decides access. Present missing or externally changed resources explicitly and require a compatible, verified definition before starting a focus session. Never infer ownership from a display name or delete unrelated domains, disks, or pool contents.

## VM and session operations

Record intent with the relevant UUIDs and volume keys before mutating libvirt. After each step, check the resulting state and record enough information to reconcile an interrupted operation on the next launch. Delete only app-owned volumes whose identities are confirmed and which are no longer attached or referenced. If ownership or rollback is uncertain, preserve the volumes and report recovery steps. Default deletion removes the domain definition with disk retention. Deleting the writable disk is a separate explicit choice.

Create an offline domain and start it on request. CPU, RAM, and disk settings apply to a stopped guest in the first implementation. Report the requested virtual disk capacity and actual host usage separately. Guest shutdown is graceful by default. A timeout leaves the VM visible as still running and offers a separately confirmed force stop. Snapshots and updates to existing disk capacity follow after the create and recovery path is verified.

The session service tracks one active focus session per host user and reads current domain state before it changes anything. It verifies the offline device policy before boot or adoption, launches the local viewer, and exposes recovery if viewer startup or the VM fails. End Session leaves the guest available for saving work. Stop VM shuts the guest down separately. See [Focus sessions and networking](sessions.md) and [Guest image and provisioning](guest.md).

## Command-line interface

| Planned command | Operation |
| --- | --- |
| `focusvm doctor` | Check host dependencies, KVM, libvirt access, storage, guest image, and desktop capabilities |
| `focusvm init` | Validate the chosen pool and write initial settings |
| `focusvm gui` | Open the manager or first-run wizard |
| `focusvm create` | Create an offline domain and per-VM overlay from a verified image |
| `focusvm start`, `focusvm stop`, `focusvm status` | Start, stop, or inspect the selected VM |
| `focusvm session start --profile deep-work` | Validate the profile, start or adopt the offline guest, and open the viewer |
| `focusvm session end` | End the session and release the viewer |
| `focusvm profiles list`, `focusvm config validate` | Inspect available profiles and check the current configuration |

Resolve the explicit VM selection or configured default to a managed UUID. Provide plain-text errors on stderr and stable JSON output for status and diagnostics. Reserve exit codes for success, invalid input, unavailable prerequisites, and operation failure. Optional integration commands appear when their workflows are implemented.
