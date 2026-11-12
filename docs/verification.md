# Verification and release

These checks define the evidence required as FocusVM is implemented. The initial reference environments are Fedora KDE Plasma on Wayland and Fedora Workstation GNOME on Wayland, both on x86_64 hosts with working KVM. [Setup](setup.md)

## Development checks

Keep package metadata, tool configuration and pytest markers in `pyproject.toml`. Run the following checks for changes to the Python package once the toolchain is in place.

| Check | What it covers |
| --- | --- |
| Ruff format and lint | Formatting, imports and configured lint rules. |
| Pyright strict | Type checking for the application package, including the typed boundaries around libvirt and Qt. Add strict Mypy checks where the dependencies and stubs support them. |
| pytest with pytest-cov | Config validation and migration, VM and session transitions, policy selection, recovery, and ownership checks on storage paths. Report coverage for the executed suite. |
| pytest-qt | Window startup, navigation for implemented pages, settings persistence, validation and error messages, and background operation updates. |
| Package build | Build a wheel and source distribution. Install the wheel in a clean environment and check entry points and bundled resources. |

Unit tests can substitute libvirt and the viewer at their application boundaries. Headless GUI tests check application behavior. Run real libvirt integration and Wayland viewer checks separately on hosts with the required capabilities, and record skipped checks and their reasons. A skipped required reference-host check leaves release verification incomplete. [pytest skip reporting](https://docs.pytest.org/en/stable/how-to/skipping.html)

Run the development checks in CI on the minimum supported Python version. Include dependency audits, dependency update checks and static security checks. Use read-only workflow permissions for routine validation and keep release credentials out of jobs that execute pull request code.

## Acceptance by milestone

1. **Foundation.** Install the package in a clean Python environment. Run CLI help, exercise configuration validation against valid and malformed files, and check that the CLI can load without importing Qt widgets. Verify that interrupted writes preserve the last valid configuration and that concurrent settings edits cannot silently discard changes.
2. **Virtualization.** Use an ordinary authorized account and `qemu:///system` to run host diagnostics, verify a signed test image, and create a disposable managed KVM domain. Inspect its overlay and seed, configure CPU and RAM while it is stopped, start it, verify its live XML has no network interface, and exercise graceful shutdown and timeout handling. Verify that interrupted creation retries preserve existing storage and reconcile a VM defined before a lost connection.
3. **Desktop manager.** Create and inspect a VM in the GUI, operate it from the CLI, and verify that both show the same libvirt state. Change settings in each frontend and restart both to verify persistence. Confirm that VM operations leave the GUI responsive and expose actionable errors when access or startup fails.
4. **Development guest.** Build the Fedora guest from the pinned installer and Kickstart, then validate its manifest and digest. Boot the VM from its own overlay and sign in with the credentials provisioned for that instance. Write a test file, shut the VM down cleanly and verify the file survives restart. Confirm that the base stays unchanged and that a second overlay starts with its own state and credentials. Edit a file with Vim, build a C++ program with Make, run it under GDB, install a local wheel into a Python virtual environment and create a local Git commit.
5. **Focus sessions.** Start and end a session through each frontend. On each reference desktop, open the guest in fullscreen through the SPICE viewer and use the documented exit controls. Confirm that ending a session closes the viewer and leaves the guest running offline. Record keyboard capture and compositor-reserved shortcuts for that desktop. Verify the default isolation of the clipboard, file transfer, host shares and device forwarding. Confirm that Strict and the initial Balanced policy boot without a guest network interface. Validate that a request for Flexible fails explicitly while it is unavailable. [Session behavior](sessions.md)
6. **Optional features.** Add acceptance checks as resource import, maintenance networking, remote Git, WakaTime, allowlisting or advanced kiosk modes are implemented. Exercise disabled and unauthorized paths as well as successful ones. Gate any claim about an optional feature on its own checks.

## Reference-host checks

Run the core acceptance flow on a clean setup on each reference desktop. Record the Fedora release, desktop and Wayland session, CPU architecture, Python, libvirt, QEMU and viewer versions, tested commit, and guest image digest. Use a disposable VM and sample repository for checks involving failed boots, interrupted operations or deletion.

Inspect the same domain UUID in the GUI, CLI and libvirt. Check the domain type and persistent configuration with `virsh -c qemu:///system dumpxml UUID --inactive`, then inspect the live configuration and interfaces after boot using `dumpxml` and `domiflist`. With the host still online, check inside the guest that it has no external interface or default route and cannot reach a test endpoint by IP or resolve an external name. Verify local Git, offline dependencies and any included offline references in that same session. Make the local wheel available on the disposable test guest before entering the session. [virsh command reference](https://libvirt.org/manpages/virsh.html)

| Failure to exercise | Required result |
| --- | --- |
| Image verification fails, storage is unwritable, or VM creation is interrupted | The error identifies the failing prerequisite. Retry safely reconciles any application-owned partial state and preserves the base image and other VMs. |
| Access to libvirt fails, or the requested offline policy cannot be verified | The frontend reports the failure. It records no active session and leaves no guest running with a weaker policy as a result of the attempt. |
| CLI and GUI start sessions concurrently | At most one operation succeeds. The other reports the conflict and both frontends show the resulting state. |
| Viewer fails to launch or closes unexpectedly | The application reports an interrupted session and the guest remains running offline. Reopen the viewer or end the session from the manager or CLI, and check that guest files survive. |
| Application is interrupted during session startup or shutdown | On the next invocation, compare recorded state with libvirt before issuing changes. Confirm the visible state matches the domain, and repeat the operation safely. |
| Guest does not respond to graceful shutdown | The frontend reports the timeout and continued VM state. A force-stop requires a separate deliberate action. |
| Removal is cancelled or an unexpected storage path is encountered | The VM and its storage survive. Verify that confirmed removal affects only the intended application-owned resources. |

## Release record

For a release candidate, build from the intended version tag and verify installation from the resulting artifacts. Record CI results, the real KVM and desktop results for both reference hosts, guest image verification, artifact checksums and known limitations. Retain only diagnostics that are suitable to share, with credentials and personal paths removed.

Publish the compatibility actually exercised, along with installation instructions, release notes, and applicable licensing information. The intended application license is MIT. Before distribution, confirm the copyright attribution and include the required notices for the guest and other bundled components. Other compatible Linux hosts can be listed as expected to work where their dependencies are available. Release requires a successful core workflow on both Fedora targets, passing automated checks, and resolution of known critical security or data-loss defects. Keep the Python package and the verified guest image separately versioned and document which guest version was tested with the release.
