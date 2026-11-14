# Security policy

## Scope and supported versions

FocusVM is in prerelease development. Report issues against current `main` or the latest release candidate and include the exact version or commit. Earlier development snapshots receive no dedicated maintenance.

## Trust and isolation

FocusVM runs as the desktop user and relies on the host's authorization for `qemu:///system`. The Linux host, its administrators, libvirt, and the local viewer are trusted. Use a guest image signed by a publisher you trust, and obtain the verification key through a separate trusted channel. FocusVM verifies the signed manifest and image digest before import. [Guest provisioning](docs/guest.md#prepare-a-vm)

Strict and Balanced sessions use a guest without a network interface. The managed VM uses local SPICE attachment with clipboard and file transfer disabled. FocusVM validates saved and running device configurations at the documented checkpoints. Administrators and other authorized libvirt clients can change a VM after those checks. Fullscreen and nested Cage support voluntary focus and provide exit controls. [Focus sessions and networking](docs/sessions.md)

Git remotes, WakaTime upload, and approved research retrieval run on the host through explicit commands outside the focus session. Git uses credentials managed by the host Git installation. WakaTime tracking is opt-in and supports key storage in the desktop keyring. Reference discs are read-only in the guest. Documents and wheels still come from sources you select and run through their normal viewers or installers. [Offline resources and synchronization](docs/resources-and-sync.md) · [Advanced sessions and maintenance](docs/advanced-sessions.md)

Research downloads contact the approved public HTTPS host and save a local Markdown file. WakaTime uploads send the project name, relative filename, and timestamp to `api.wakatime.com`. Unconfirmed events stay in the local queue until a successful retry or explicit removal. Review the queue before uploading. [WakaTime controls](docs/resources-and-sync.md#wakatime-activity)

## Report a vulnerability

If **Report a vulnerability** is available on the repository's GitHub Security page, submit a private report there. If the option is unavailable, open an issue asking the maintainers for a private contact and keep the vulnerability details for that channel. [Repository security advisories](https://github.com/ChecksMc/FocusVM/security/advisories) · [GitHub private reporting instructions](https://docs.github.com/en/code-security/how-tos/report-and-fix-vulnerabilities/report-privately)

Include the affected release or commit, the host and guest versions, the relevant configuration, the observed behavior, and its impact. Use a disposable environment to reproduce the issue and remove credentials, personal filenames, and guest data from attachments. Coordinate publication with the maintainers so affected users can receive a fix or workaround.
