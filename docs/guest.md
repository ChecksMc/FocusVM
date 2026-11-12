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

The wizard and CLI use the same provisioning path.

1. Select a supported image release or import its local image and signed manifest. Stage the download privately, verify the manifest signature and image digest, and validate the expected QCOW2 metadata before importing it. Accept standalone base images without backing or external data files. A failed verification stops provisioning and leaves existing VMs untouched.
2. Import the verified base into the selected libvirt pool under its digest. Treat it as read-only, create a writable QCOW2 overlay for the VM, and use the recorded base format and volume identity in the backing chain. Each VM has its own overlay. The base remains in the pool while any overlay depends on it. [Libvirt storage volume and backing store XML](https://libvirt.org/formatstorage.html#backing-store-elements)
3. Generate a private NoCloud seed with the domain's unique instance ID and a password hash for the guest-local user. Prompt for the password in the GUI or without echo in the CLI. Keep the plaintext out of command arguments, configuration, and logs. Attach the seed read-only. Retain the seed for a retry until guest initialization is confirmed, then remove it from the domain and pool when the VM is stopped. [NoCloud local seed media](https://docs.cloud-init.io/en/latest/reference/datasources/nocloud.html)
4. Define the guest with the overlay and seed, local SPICE graphics, and no NIC. On first boot the guest configures its account and grows the guest filesystem to the configured capacity when needed. The wizard guides the user through the first login and records completion after the user confirms it. An interrupted first boot preserves the overlay for diagnosis or retry.

Releasing a new base image creates a new version for new VMs. Existing overlays continue to use the exact base they were built on. Provide an export or backup path before adding migrations between base versions. During development, test rebuilds against disposable overlays and keep user data outside test volumes.

## Tools and reference material

Users work in guest-local directories and can create Python virtual environments and commit changes with Git while offline. The base includes man pages. Add a read-only import path for offline wheels and local HTML, PDF, and ZIM collections after the initial session workflow is stable. Use existing viewers and Kiwix where appropriate. Keep larger archives and the optional scientific Python preset separate from the default image. Controlled guest package installation belongs in an explicitly started maintenance workflow outside focus sessions.
