# Verification and release

These checks define the evidence required as FocusVM is implemented. The initial reference environments are Fedora KDE Plasma on Wayland and Fedora Workstation GNOME on Wayland, both on x86_64 hosts with working KVM. [Setup](setup.md)

## Development checks

Keep package metadata, tool configuration and pytest markers in `pyproject.toml`. Run the following checks for changes to the Python package once the toolchain is in place.

| Check | What it covers |
| --- | --- |
| Ruff format and lint | Formatting, imports and configured lint rules. |
| Pyright strict | Type checking for the application package, including the typed boundaries around libvirt and Qt. Add strict Mypy checks where the dependencies and stubs support them. |
| pytest with pytest-cov | Config validation and migration, VM and session transitions, policy selection, recovery, and ownership checks on storage paths. Report coverage for the executed suite. |
| pytest-qt | Window startup, navigation, settings persistence, setup validation and errors, and background VM operations. CI uses the Qt offscreen platform for these tests. |
| Package build | Build a wheel and source distribution. Install the wheel in a clean environment and check entry points and bundled resources. |

Unit tests can substitute libvirt and the viewer at their application boundaries. Headless GUI tests check application behavior. Run real libvirt integration and Wayland viewer checks separately on hosts with the required capabilities, and record skipped checks and their reasons. A skipped required reference-host check leaves release verification incomplete. [pytest skip reporting](https://docs.pytest.org/en/stable/how-to/skipping.html)

CI tests Python 3.12 and 3.14 and requires 75% coverage with branch measurement enabled. Its package check builds from a clean Git archive, checks the archive contents, and installs both distributions in separate environments. The hosted workflow covers package and offscreen Qt tests. Native KVM and Wayland checks use the manual record below.

## Acceptance by milestone

1. **Foundation.** Install the package in a clean Python environment. Run CLI help, exercise configuration validation against valid and malformed files, and check that the CLI can load without importing Qt widgets. Verify that interrupted writes preserve the last valid configuration and that concurrent settings edits cannot silently discard changes.
2. **Virtualization.** Use an ordinary authorized account and `qemu:///system` to run host diagnostics, verify a signed test image, and create a disposable managed KVM domain. Inspect its overlay and seed, configure CPU and RAM while it is stopped, start it, verify its live XML has no network interface, and exercise graceful shutdown and timeout handling. Verify that interrupted creation retries preserve existing storage and reconcile a VM defined before a lost connection.
3. **Desktop manager.** Create and inspect a VM in the GUI, operate it from the CLI, and verify that both show the same libvirt state. Change settings in each frontend and restart both to verify persistence. Confirm that VM operations leave the GUI responsive and expose actionable errors when access or startup fails.
4. **Development guest.** Build the Fedora guest from the pinned installer and Kickstart, then validate its manifest and digest. Boot the VM from its own overlay and sign in with the credentials provisioned for that instance. Write a test file, shut the VM down cleanly and verify the file survives restart. Confirm that the base stays unchanged and that a second overlay starts with its own state and credentials. Edit a file with Vim, build a C++ program with Make, run it under GDB, install a local wheel into a Python virtual environment and create a local Git commit.
5. **Focus sessions.** Start and end a session through each frontend. On each reference desktop, open the guest in fullscreen through the SPICE viewer and use the documented exit controls. Close the viewer and check that another invocation reports a disconnected session, then reopen it. Confirm that ending closes only the session viewer and leaves the guest running offline. Record keyboard capture and compositor-reserved shortcuts for that desktop. Verify the default isolation of the clipboard, file transfer, host shares and device forwarding. Confirm that Strict and Balanced boot without a guest network interface. Validate that a request for Flexible fails explicitly while it is unavailable. [Session behavior](sessions.md)
6. **Documentation and synchronization.** Import and find a PDF and a ZIM file, attach them to a stopped VM, and boot without a NIC. Check that the disc and seed are read-only, open the PDF, look up a C++ standard library entry, and read a ZIM through the guest's loopback server. Shut down and eject the reference disc. Transfer a disposable Git branch in both directions, check separate review refs, and verify a divergent push cannot overwrite history. Test guest extraction with `guestfish` when installed. Verify that WakaTime recording is opt-in, upload is unavailable during focus sessions, and unconfirmed events stay queued. Use a test account for live keyring and upload checks. [Offline resources and synchronization](resources-and-sync.md)
7. **Advanced sessions.** Verify Contest and Research use the offline guest, and that restarting the viewer preserves the timer and leaves the VM running at zero. Test the research allowlist with an approved host, a redirect, and a nonpublic DNS answer. Attach trusted wheels and install them offline in a new project virtual environment. Verify Cage startup, viewer exit, independent session end, and reopen on Wayland. End a session with a completion note. Confirm the journal records its elapsed time once and includes manual entries in the totals. [Advanced sessions and maintenance](advanced-sessions.md)
8. **Production hardening.** Check recovery from interrupted VM creation, rejected device and disk configurations, failed viewer startup, and a timed-out shutdown. Hold background status checks in progress while using the desktop controls. Check that malformed configuration still produces a full diagnostic report and that shared or linked private state is rejected. Run the core viewer and recovery flow on both Fedora reference desktops.

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

### Phase 8 development checks

The Qt manager and CLI completed Strict and Balanced session checks using disposable KVM guests on Fedora 44 x86-64. The checks used Python 3.14.10, libvirt 12.0.0, QEMU 10.2.2, Qt for Python 6.11.2, and virt-viewer 11.0. Both runs checked the viewer's Wayland fullscreen request, the live VM's device isolation and SPICE sharing settings, detection and recovery after viewer closure, session end, and separate guest shutdown.

| Display environment | Result |
| --- | --- |
| KDE Plasma 6.8.2 in the host Wayland session | Manager and CLI session and recovery checks passed. |
| GNOME Shell 50.6 and Mutter 50.6 on an isolated virtual Wayland monitor | The same manager and CLI checks passed. A Fedora Workstation login and physical keyboard shortcut checks are still pending. |

The desktop smoke checks used a small disposable boot image. The Fedora development image is exercised separately with its offline development and resource checks.

## Manual acceptance record

For each candidate, run the reference checks on both Fedora desktop logins. Repeat them when changing the guest recipe, Qt, QEMU, libvirt, or viewer versions. Launch a disposable VM from a verified guest and use the manager and CLI to check start, input release, fullscreen toggle, disconnect, reopen, session end, and graceful shutdown. Verify that the guest has no external interface after boot. In a separate trial, exercise the package and resource workflows. Record `pass`, `fail`, or `pending` with a reason and attach a scrubbed transcript to the release review.

| Record | Value to capture |
| --- | --- |
| Build | Exact commit/tag, wheel checksum, guest version and QCOW2 checksum |
| Environment | Fedora release and desktop, session type, Python, Qt, libvirt, QEMU and viewer versions |
| Core flow | Create, verify offline boot, use and release input, recover the viewer, end, stop |
| Offline tools | Guest verification, selected reference media, local Git and wheel install |
| Failures and skipped checks | State and reason, with the relevant redacted diagnostics |

Use `focusvm doctor --redact --json` for the shareable check summary. Review separate command traces and omit guest data, usernames, credentials, and local paths.

### 0.1.0rc1 candidate checks

The local Python 3.12.16 workflow passed 191 tests, Ruff, Mypy, Pyright, and the dependency audit. On Fedora 44 KDE Wayland, the installed candidate wheel imported the Fedora 44.2 development image using a disposable test signature. The manager and CLI booted it offline, reopened the fullscreen viewer after it closed, ended the session, and shut down the guest separately. The development image SHA-256 is `546968488fe44834d6aae4c489a1e935ac110d6ff5a052d89ea58c868c2793c5`.

A hosted GitHub Actions result for the candidate and the full Fedora Workstation login and keyboard checks are pending. The local acceptance run used a temporary signer. A distributable signed guest requires a publisher key and a redistribution review. [Release procedure](release.md)

## Release record

For a release candidate, build from the intended version tag and verify installation from the resulting artifacts. Record CI results, the real KVM and desktop results for both reference hosts, guest image verification, artifact checksums and known limitations. Retain only diagnostics that are suitable to share, with credentials and personal paths removed.

Publish the compatibility actually exercised, along with installation instructions, release notes, and applicable licensing information. The host application uses MIT. Include the required notices for any distributed guest. A general-use release requires a successful core workflow on both Fedora targets, passing automated checks, and resolution of known critical security or data-loss defects. Keep the Python package and the verified guest image separately versioned and document which guest version was tested with the release.
