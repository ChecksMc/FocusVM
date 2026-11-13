# Architecture

FocusVM uses one local QEMU/KVM backend through libvirt. The desktop manager and CLI run as the signed-in user and call the same Python services. The initial reference host is x86_64 Linux with KVM. The first compatibility runs target Fedora KDE Plasma and Fedora Workstation GNOME on Wayland. The guest has its own desktop and does not inherit the host desktop or package manager. [libvirt connection URIs](https://libvirt.org/uri.html)

## Package structure

Add modules when their first feature lands. The initial Python package uses a `src` layout and `pyproject.toml` for packaging and development tooling.

```text
src/focusvm/
  config.py          Settings, profile validation, and TOML migrations
  cli.py             Command parsing, text and JSON output, and exit codes
  local_state.py     Private files, locking, and atomic writes
  diagnostics.py     Host and dependency checks
  vm.py              Managed VM operations and creation inventory
  virtualization.py  Libvirt connection, domain definitions, and storage
  provisioning.py    Image verification and per-VM preparation
  sessions.py        Session transitions, offline policy, and recovery
  viewer.py          Local virt-viewer launch and exit handling
  gui/               PySide6 manager and first-run wizard
guest/               Guest build recipe and image manifest tooling
tests/               Unit, GUI, and KVM integration suites
```

Both frontends use the shared configuration, diagnostics, and VM services. The core remains free of Qt imports. The Qt manager runs libvirt, image, and file operations on a background thread and receives results through Qt signals. Its pages cover the dashboard, VM management, and settings. The viewer command builder supplies the attached local console used by the manager. Later sessions will coordinate VM policy and viewer lifecycle.

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

Schema version 1 requires the `application` and `vm_defaults` tables and an `application.storage_pool` name. The remaining preferences use the defaults below. `default_vm` is an optional libvirt domain UUID and `deep-work` is the currently accepted profile ID. `init` checks the pool name's format. `doctor` and `create` check the actual pool in libvirt.

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

The configuration service rejects invalid values, unknown fields, and unsupported schema versions. Add migrations when a new schema version ships. A migration must validate the new document before atomically replacing the old one and keep a recoverable copy. Configuration writes stage the new file in the same directory before replacing the destination. A file lock serializes writes, and the update API reads current settings under the lock before applying a change. The future GUI will use that same API.

Libvirt owns actual VM status, resource allocations, device configuration, and volume identity. The local inventory stores managed domain UUIDs and volume keys. Cross-check inventory against FocusVM metadata in the domain before acting. The metadata identifies app-managed resources and libvirt still decides access. Present missing or externally changed resources explicitly and require a compatible, verified definition before starting a focus session. Never infer ownership from a display name or delete unrelated domains, disks, or pool contents.

## VM and session operations

`vms.json` under the XDG state directory records the owner token and VM UUID before creation touches libvirt. The service checkpoints base, overlay, and seed volume keys as they become known. A retry with the same inputs adopts an already-defined matching domain. If libvirt confirms that an incomplete domain is absent, the service records the attempt as abandoned and allocates a fresh UUID. A previously completed VM that disappears is reported as missing. Interrupted attempts preserve their volume records and storage for recovery.

Create defines an offline domain. Start verifies the saved definition, launches the VM paused, and checks the live devices before letting the guest run. `configure` changes CPU and RAM only while the guest is stopped. Disk capacity is chosen at creation and status reports both virtual capacity and allocated bytes. Stop requests a graceful shutdown and leaves the VM visible if the request times out. Force stop, deletion, snapshots, and changes to an existing disk's capacity are later features.

The session service tracks one active focus session per host user and reads current domain state before it changes anything. It verifies the offline device policy before boot or adoption, launches the local viewer, and exposes recovery if viewer startup or the VM fails. End Session leaves the guest available for saving work. Stop VM shuts the guest down separately. See [Focus sessions and networking](sessions.md) and [Guest image and provisioning](guest.md).

## Command-line interface

| Command | Status | Operation |
| --- | --- | --- |
| `focusvm init --storage-pool NAME` | Available | Write initial settings or report that identical settings already exist |
| `focusvm config validate` | Available | Check the current configuration |
| `focusvm doctor [--json]` | Available | Check host dependencies, KVM, libvirt access, storage, and desktop capabilities |
| `focusvm gui` | Available with the `gui` extra | Open the manager and launch setup when settings are absent |
| `focusvm create` | Available | Create an offline domain and per-VM overlay from a verified local image |
| `focusvm list` | Available | List VM UUIDs and creation phases from the local inventory |
| `focusvm start`, `focusvm stop`, `focusvm status` | Available | Start, stop, or inspect the selected VM |
| `focusvm configure` | Available | Change CPU and RAM on a stopped VM |
| `focusvm session start --profile deep-work` | Planned | Validate the profile, start or adopt the offline guest, and open the viewer |
| `focusvm session end` | Planned | End the session and release the viewer |
| `focusvm profiles list` | Planned | Inspect available profiles |

Resolve the explicit VM selection or configured default to a managed UUID. Provide plain-text errors on stderr and stable JSON output for status and diagnostics. Reserve exit codes for success, invalid input, unavailable prerequisites, and operation failure. Optional integration commands appear when their workflows are implemented.
