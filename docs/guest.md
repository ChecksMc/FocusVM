# Guest image and provisioning

The first guest is an x86_64 Fedora image with a lightweight Xfce desktop, a terminal, Vim, Git, GCC/G++, Make, GDB, C++ standard library headers, Python, pip, virtual environments, and local man pages. It starts with a regular local user and uses the same image on both Fedora host desktops. The Python environment inside the guest is independent of the host's Python environment.

## Build the base image

Build the distributable QCOW2 on a dedicated build machine or runner. Use the Fedora Server network installation ISO and a checked-in Kickstart recipe in a disposable libvirt VM. The recipe installs only the selected desktop components, development tools, cloud-init, and any local display or shutdown support the guest needs. The builder uses network access for verified build inputs and signed packages. User focus VMs boot without a network interface. [Fedora Server downloads](https://fedoraproject.org/server/download/) · [Pykickstart documentation](https://pykickstart.readthedocs.io/en/latest/kickstart-docs.html)

1. Pin the Fedora release and installation-media digest. Verify the signed Fedora checksum and the media before booting the installer. Record the Kickstart revision, installer architecture, build tool versions, and package repository snapshot. [Fedora image verification](https://fedoraproject.org/security/)
2. Install into a new blank disk attached only to the disposable build VM. Use signed package repositories and capture the installed package versions. Set up a partition and filesystem layout that supports offline first-boot expansion when the overlay is larger than the base disk.
3. Prepare the template for cloning. Lock build-time accounts, clear installer and cloud-init state that would otherwise be carried into clones, and regenerate machine-specific identity in each guest. Configure cloud-init to use the local NoCloud source with guest network configuration disabled. Disable SSH in the initial image. [NoCloud data source](https://docs.cloud-init.io/en/latest/reference/datasources/nocloud.html)
4. Export a standalone QCOW2 with no external backing file. Boot and verify a disposable overlay, including an offline first boot, local login, guest-visible disk size, and the development toolchain. Record the virtual disk size and boot firmware requirements so the VM definition can match the image. [QEMU disk image utility](https://www.qemu.org/docs/master/tools/qemu-img.html)
5. Publish the tested image with a signed release manifest containing its digest, architecture, format, virtual size, firmware requirements, and version. Ship a trusted release verification key with the app and document how users can verify it. A repeat build uses the same recipe and recorded inputs. The published digest identifies the actual tested bytes.

## Prepare a VM

The CLI implements local image provisioning. The future wizard will use the same path.

1. Supply a local image, manifest, detached signature, and trusted binary OpenPGP keyring. FocusVM stages a private copy, verifies the manifest signature and image digest, and checks the QCOW2 metadata and integrity before importing it. The base must be standalone, unencrypted, and free of external data files.
2. Import the verified base into the selected libvirt pool under its digest. Treat it as read-only, create a writable QCOW2 overlay for the VM, and use the recorded base format and volume identity in the backing chain. Each VM has its own overlay. The base remains in the pool while any overlay depends on it. [Libvirt storage volume and backing store XML](https://libvirt.org/formatstorage.html#backing-store-elements)
3. Generate a private NoCloud seed with the domain's unique instance ID and a password hash for the `focusvm` guest user. The CLI prompts without echo and sends the password to the hash tool on standard input. Attach the seed read-only. Phase 2 keeps the seed attached for retries. The wizard can remove it after first boot has been confirmed in a later milestone. [NoCloud local seed media](https://docs.cloud-init.io/en/latest/reference/datasources/nocloud.html)
4. Define the guest with the overlay and seed, local SPICE graphics, and no NIC. First-boot account configuration and filesystem growth depend on the chosen guest image's cloud-init and partition layout. An interrupted first boot preserves the overlay for diagnosis or retry.

Releasing a new base image creates a new version for new VMs. Existing overlays continue to use the exact base they were built on. Provide an export or backup path before adding migrations between base versions. During development, test rebuilds against disposable overlays and keep user data outside test volumes.

## Phase 2 image manifest

The manifest is JSON with these fields. `version` identifies the publisher's build. `sha256` is the lowercase SHA-256 digest of the exact QCOW2 file. `virtual_size_bytes` is the disk capacity reported by `qemu-img info`, independent of its file size. The current importer accepts x86-64 BIOS QCOW2 images. Sign the manifest as a detached OpenPGP signature and obtain the trusted publisher key separately from the image download. [QEMU disk image utility](https://www.qemu.org/docs/master/tools/qemu-img.html) · [GnuPG `gpgv`](https://www.gnupg.org/documentation/manuals/gnupg/gpgv.html)

| Field | Expected value |
| --- | --- |
| `manifest_version` | `1` |
| `version` | Nonempty build identifier |
| `sha256` | 64 lowercase hexadecimal characters |
| `architecture` | `x86_64` |
| `format` | `qcow2` |
| `virtual_size_bytes` | Positive integer |
| `firmware` | `bios` |

## Tools and reference material

Users work in guest-local directories and can create Python virtual environments and commit changes with Git while offline. The base includes man pages. Add a read-only import path for offline wheels and local HTML, PDF, and ZIM collections after the initial session workflow is stable. Use existing viewers and Kiwix where appropriate. Keep larger archives and the optional scientific Python preset separate from the default image. Controlled guest package installation belongs in an explicitly started maintenance workflow outside focus sessions.
