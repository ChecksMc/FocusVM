# Focus sessions, profiles, and networking

Deep Work and the two offline network policies are available in the CLI and desktop manager. See [Architecture](architecture.md) for state ownership and [Setup](setup.md) for the host and guest prerequisites.

## Profiles

The first usable build targets **Deep Work**. It opens the guest fullscreen with a C++ and Python toolchain, Vim, Git, GDB, and local manual pages. It defaults to Balanced networking, disabled clipboard and file transfer, and an open-ended session.

**Contest** follows with local problem statements, compiler and test access, and Strict networking. Its optional timer alerts the user without stopping the VM. **Research** adds local PDF and ZIM collections, notes, and the optional scientific Python preset. Research begins with Balanced networking and gains approved online references when the network policy supports them.

Built-in profiles have stable IDs and can be duplicated for customization. Custom profiles will be stored as versioned TOML and managed through the same validation rules in the CLI and desktop manager. A profile selects the network policy, display and clipboard preferences, an optional timer, and a list of resource collection IDs. Guest tool presets are provisioning inputs. Starting a session checks that its requested capabilities and collections are available. A session records its effective profile so later edits take effect on the next start. Imports validate the schema and referenced collections before saving. Profile exports contain settings and collection IDs only.

## Offline networking

| Policy | Current behavior | Planned extensions |
| --- | --- | --- |
| Strict | The guest has no virtual network interface. Local tools, Git, and resources remain usable. | Package installs use preloaded artifacts. Remote synchronization waits for an explicit action outside the session. |
| Balanced (default) | The same offline guest configuration as Strict. | Explicit Git, WakaTime, and package operations outside the session. Optional approved research access requires an implemented policy. |
| Flexible | Unavailable. Selecting it returns a capability error. | Access to configured destinations through a dedicated, tested guest egress policy. |

The baseline managed domain has no virtual network interfaces, network-capable passthrough devices, or USB redirection. The first build needs no app-created libvirt network or host firewall rules. Validate the saved libvirt definition before starting the guest and check its live configuration before presenting the session as offline. For a guest that is already running, require both definitions to match the requested controls before opening its console. Reject an unexpected device or a managed-save image that could restore stale devices. Configuration and libvirt errors surface in both interfaces. [Libvirt domain XML](https://libvirt.org/formatdomain.html), [libvirt domain state API](https://libvirt.org/html/libvirt-libvirt-domain.html#virDomainHasManagedSaveImage).

Graphics use SPICE with a libvirt-provided connection and `<listen type='none'/>`. That setting gives the viewer no listening endpoint to fall back to if its direct libvirt attachment fails. Set SPICE clipboard and file transfer to disabled by default and start with no host filesystem shares. Planned resource collections can use read-only media or deliberately scoped shares. Clipboard and shared-folder settings are independent of network policy and appear in the session summary when enabled. [Libvirt graphics and device configuration](https://libvirt.org/formatdomain.html#graphical-framebuffers), [virt-viewer manual](https://gitlab.com/virt-viewer/virt-viewer/-/raw/master/man/virt-viewer.pod).

Flexible networking must define what a destination rule matches, how names and IPv4 and IPv6 are handled, and which traffic is denied by default. Test resolution changes, direct-address connections, and failure recovery before offering the policy. Any host changes must be scoped to the managed guest and removed on exit or recovery. Guest networking and fullscreen are controls for a voluntary focus session. Host administrators retain control of libvirt and their desktop.

## Start, end, and recover

The GUI and CLI use the same session service, with one focus session at a time per host user. Session state is distinct from VM state. For example, a disconnected session can have a running offline guest.

1. **Start.** In the manager, select a VM in **Virtual Machines**, open **Focus Sessions**, choose Balanced or Strict, and select **Start Focus Session**. The CLI equivalent is `focusvm session start --profile deep-work --network balanced`. Add `--vm UUID` to override the configured default or sole managed VM. Both frontends check the offline policy and save a private session record before boot or viewer launch.
2. **Exit fullscreen.** Use **Shift+F12** to release input and **Shift+F11** to toggle fullscreen. The viewer's window controls also let you close it. Wayland compositors can reserve Alt+Tab, Super, or other shortcuts. [virt-viewer manual](https://gitlab.com/virt-viewer/virt-viewer/-/raw/master/man/virt-viewer.pod) · [Wayland shortcut inhibition](https://raw.githubusercontent.com/wayland-mirror/wayland-protocols/main/unstable/keyboard-shortcuts-inhibit/keyboard-shortcuts-inhibit-unstable-v1.xml)
3. **End.** Use **End Focus Session** or `focusvm session end` in a host terminal. Ending closes the identified fullscreen viewer and clears the session record. The guest keeps its current power state so unsaved work stays accessible. To shut down the guest, use **Shut Down** or `focusvm stop` separately.
4. **Recover.** Use `focusvm session status` or the manager to see whether the viewer is active, disconnected, or unresolved. **Reopen Viewer** or `focusvm session reopen` rechecks offline policy and reconnects, booting the guest if it was stopped. End the existing session before starting another. If the viewer cannot be identified safely, close its window directly and retry End. Viewer errors include the log location under `$XDG_STATE_HOME/focusvm/focus-viewer.log`.

The viewer runs independently of the CLI or manager that launched it. The session record and process identity let a later invocation recover it. FocusVM uses Linux pidfds for safe process signaling. [Python pidfd API](https://docs.python.org/3.12/library/os.html#os.pidfd_open)

Ordinary fullscreen uses the viewer's regular quit controls. Advanced kiosk integration with Cage or a dedicated Wayland session is a later option contingent on a tested emergency exit and safe recovery after viewer or compositor failure.

## Work outside a session

Git commits, branches, diffs, and history work inside the offline guest. A later synchronization flow can transfer commits through an explicit Git bundle import or export and use host-side Git for a user-approved remote operation. The host keeps its Git credentials. Check the repository, remote, ref, and working state before applying updates. Preserve local changes and report divergence for the user to resolve.

For dependencies, build or fetch wheels and other artifacts in a separate maintenance step, then install from local media in the guest. Python packages go in a project virtual environment. Any future networked maintenance mode requires an explicit start outside a focus session and an offline configuration check before the next session.

WakaTime and a local session journal are opt-in additions. Tracking should expose the selected projects and queued activity before synchronization, use a host secret store for the API key, and support user-triggered upload outside focus sessions. The journal can record project, duration, references, and notes locally. [WakaTime offline behavior](https://wakatime.com/faq).
