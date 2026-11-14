# Focus sessions, profiles, and networking

Deep Work, Contest, Research, and the two offline network policies are available in the CLI and desktop manager. See [Architecture](architecture.md) for state ownership and [Setup](setup.md) for the host and guest prerequisites.

## Profiles

**Deep Work** opens the guest fullscreen with a C++ and Python toolchain, Vim, Git, GDB, and local manual pages. It defaults to Balanced networking, disabled clipboard and file transfer, and an open-ended session.

**Contest** defaults to Strict networking. Attach local problem statements and choose an optional countdown that leaves the VM running at zero. **Research** defaults to Balanced and uses the same guest tools with any attached PDFs, notes, or ZIM archives. Approved online references are downloaded on the host before the session. [Advanced sessions and maintenance](advanced-sessions.md)

Built-in profile IDs are `deep-work`, `contest`, and `research`. The session record stores the effective profile, network policy, timer, and viewer choice. Profile editing, imported custom profiles, and per-profile resource collections are planned.

## Offline networking

| Policy | Guest networking | Host operations |
| --- | --- | --- |
| Strict | The guest has no virtual network interface. Local tools, Git, and resources remain usable. | Package installs use preloaded artifacts. Remote synchronization waits for an explicit action outside the session. |
| Balanced (default) | The same offline guest configuration as Strict. | Explicit Git, WakaTime, package, and approved research operations on the host outside the session. |
| Flexible | Unavailable. Selecting it returns a capability error. | Access to configured destinations through a dedicated, tested guest egress policy. |

The baseline managed domain has no virtual network interfaces, network-capable passthrough devices, or USB redirection. The first build needs no app-created libvirt network or host firewall rules. Validate the saved libvirt definition before starting the guest and check its live configuration before presenting the session as offline. For a guest that is already running, require both definitions to match the requested controls before opening its console. Reject an unexpected device or a managed-save image that could restore stale devices. Configuration and libvirt errors surface in both interfaces. [Libvirt domain XML](https://libvirt.org/formatdomain.html), [libvirt domain state API](https://libvirt.org/html/libvirt-libvirt-domain.html#virDomainHasManagedSaveImage).

VM startup verifies the overlay's backing image against the local inventory. New domains explicitly name the one verified backing image. Earlier domains can start paused with a backing chain discovered by libvirt, then resume after the live chain is checked. Subsequent checks detect configuration changes made through another libvirt client.

Graphics use SPICE with a libvirt-provided connection and `<listen type='none'/>`. That setting gives the viewer no listening endpoint to fall back to if its direct libvirt attachment fails. Set SPICE clipboard and file transfer to disabled by default and start with no host filesystem shares. Planned resource collections can use read-only media or deliberately scoped shares. Clipboard and shared-folder settings are independent of network policy and appear in the session summary when enabled. [Libvirt graphics and device configuration](https://libvirt.org/formatdomain.html#graphical-framebuffers), [virt-viewer manual](https://gitlab.com/virt-viewer/virt-viewer/-/raw/master/man/virt-viewer.pod).

Flexible networking must define what a destination rule matches, how names and IPv4 and IPv6 are handled, and which traffic is denied by default. Test resolution changes, direct-address connections, and failure recovery before offering the policy. Any host changes must be scoped to the managed guest and removed on exit or recovery. Guest networking and fullscreen are controls for a voluntary focus session. Host administrators retain control of libvirt and their desktop.

## Start, end, and recover

The GUI and CLI use the same session service, with one focus session at a time per host user. Session state is distinct from VM state. For example, a disconnected session can have a running offline guest.

1. **Start.** In the manager, select a VM in **Virtual Machines**, open **Focus Sessions**, choose Balanced or Strict, and select **Start Focus Session**. The CLI equivalent is `focusvm session start --profile deep-work --network balanced`. Add `--vm UUID` to override the configured default or sole managed VM. Both frontends check the offline policy and save a private session record before boot or viewer launch.
2. **Exit fullscreen.** Use **Shift+F12** to release input and **Shift+F11** to toggle fullscreen. The viewer's window controls also let you close it. Wayland compositors can reserve Alt+Tab, Super, or other shortcuts. [virt-viewer manual](https://gitlab.com/virt-viewer/virt-viewer/-/raw/master/man/virt-viewer.pod) · [Wayland shortcut inhibition](https://raw.githubusercontent.com/wayland-mirror/wayland-protocols/main/unstable/keyboard-shortcuts-inhibit/keyboard-shortcuts-inhibit-unstable-v1.xml)
3. **End.** Use **End Focus Session** or `focusvm session end` in a host terminal. Ending closes the identified fullscreen viewer and clears the session record. The guest keeps its current power state so unsaved work stays accessible. To shut down the guest, use **Shut Down** or `focusvm stop` separately.
4. **Recover.** Use `focusvm session status` or the manager to see whether the viewer is active, disconnected, or unresolved. **Reopen Viewer** or `focusvm session reopen` rechecks offline policy and reconnects, booting the guest if it was stopped. End the existing session before starting another. If the viewer cannot be identified safely, close its window directly and retry End. Viewer errors include the log location under `$XDG_STATE_HOME/focusvm/focus-viewer.log`.

The viewer runs independently of the CLI or manager that launched it. The session record and process identity let a later invocation recover it. FocusVM uses Linux pidfds for safe process signaling. [Python pidfd API](https://docs.python.org/3.12/library/os.html#os.pidfd_open)

Ordinary fullscreen uses the viewer's regular quit controls. On a local Wayland desktop, the optional Cage viewer runs in a nested host window and can be ended from the manager or a separate host terminal. [Optional Cage viewer](advanced-sessions.md#optional-cage-viewer)

## Work outside a session

Git commits, branches, diffs, and history work inside the offline guest. Use bundles to transfer commits and run host-side fetch or push after ending the session. [Git transfer and synchronization](resources-and-sync.md#git)

For dependencies, attach an offline wheel disc outside a focus session, then install those wheels into a project virtual environment. Research references can be retrieved from approved sites on the host and attached the same way. [Advanced sessions and maintenance](advanced-sessions.md)

WakaTime activity is opt-in and can be queued locally. It uploads only when requested after the session ends. The local journal records sessions you add manually. [WakaTime activity](resources-and-sync.md#wakatime-activity) · [Journal and statistics](advanced-sessions.md#journal-and-statistics)
