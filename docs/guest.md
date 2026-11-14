# Guest image and provisioning

The Fedora 44 guest targets x86-64 BIOS. It contains Xfce, LightDM, a terminal, Vim, Git, GCC/G++, Make, GDB, Python with pip and virtual environments, and offline reference tools. First boot creates the regular `focusvm` account from the VM's NoCloud seed. The guest's tools and desktop are independent of the host desktop.

## Build the base image

Run the builder as an ordinary user on an x86-64 Linux host with working `qemu:///session` KVM, QEMU SPICE support, `passt`, the libvirt Python bindings, `qemu-img`, `xorriso`, `gpgv`, and OpenSSL. Use the same Python environment as FocusVM. Allow at least 10 GiB for installer and build files. The disposable installer uses user-mode networking to reach the Fedora 44 release tree. The offline acceptance VM has no network interface. [Libvirt user-mode networking](https://libvirt.org/formatdomain.html#userspace-connection-using-passt) · [Anaconda boot options](https://anaconda-installer.readthedocs.io/en/latest/user-guide/boot-options.html)

Download `Fedora-Server-netinst-x86_64-44-1.7.iso` and `Fedora-Server-44-1.7-x86_64-CHECKSUM` from Fedora's release directory. Obtain Fedora's binary OpenPGP keyring separately. The builder pins the Fedora 44 signing fingerprint and the exact installer digest. Package selection comes from Fedora's HTTPS release tree and the build records the installed versions. [Fedora 44 Server release files](https://dl.fedoraproject.org/pub/fedora/linux/releases/44/Server/x86_64/iso/) · [Fedora signing keys and verification](https://fedoraproject.org/security/)

```sh
PYTHONPATH=src python3 -m guest.build \
  --iso Fedora-Server-netinst-x86_64-44-1.7.iso \
  --checksum Fedora-Server-44-1.7-x86_64-CHECKSUM \
  --fedora-keyring fedora.gpg \
  --version fedora-44.2 --output guest-output/fedora-44.2
```

The builder verifies the installer, applies [the Kickstart recipe](../guest/fedora-44.ks) to a new disk, and compacts the result to a standalone 12 GiB virtual-capacity QCOW2. Kickstart locks root, disables SSH, configures graphical boot, and enables NoCloud and root filesystem expansion. An enlarged offline overlay runs [the guest verifier](../guest/verify.sh) before the builder emits `guest.qcow2`, `guest.json`, `packages.txt`, and `build-inputs.json`. `work/install.log` and `work/verify.log` contain the serial output. The `work` directory also retains intermediate inputs for diagnosing the build. [NoCloud data source](https://docs.cloud-init.io/en/latest/reference/datasources/nocloud.html) · [QEMU disk image utility](https://www.qemu.org/docs/master/tools/qemu-img.html)

For import, add `--sign-with FINGERPRINT` to produce `guest.sig` with an available OpenPGP signing key. Export that key's public certificate in a binary keyring and give the importer a trusted copy. The unsigned output is useful for build inspection. The repository has no published image or project signing key yet.

## Prepare a VM

The CLI and desktop manager use the same local image provisioning service.

1. Supply a local image, manifest, detached signature, and trusted binary OpenPGP keyring. FocusVM stages a private copy, verifies the manifest signature and image digest, and checks the QCOW2 metadata and integrity before importing it. The base must be standalone, unencrypted, and free of external data files.
2. Import the verified base into the selected libvirt pool under its digest. Treat it as read-only, create a writable QCOW2 overlay for the VM, and use the recorded base format and volume identity in the backing chain. Each VM has its own overlay. The base remains in the pool while any overlay depends on it. [Libvirt storage volume and backing store XML](https://libvirt.org/formatstorage.html#backing-store-elements)
3. Generate a private NoCloud seed with the domain's unique instance ID and a password hash for the `focusvm` guest user. The CLI prompts without echo and the desktop manager masks the input. The hash tool receives the password on standard input. The VM keeps the seed attached read-only for retries. [NoCloud local seed media](https://docs.cloud-init.io/en/latest/reference/datasources/nocloud.html)
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

Users work in guest-local directories and can create Python virtual environments and commit changes with Git while offline. The base includes man pages and the GNU C++ standard library documentation. Attach a read-only disc with offline wheels or HTML, PDF, and ZIM references while the VM is stopped. The image recipe includes Atril, Firefox, and Kiwix tools. Import larger reference archives when needed. [Offline resources and synchronization](resources-and-sync.md)

Run `bash /path/to/verify.sh` as `focusvm` from Xfce Terminal to repeat the checks inside a guest. The verifier uses a temporary working directory to compile and debug a sample, create a local Git commit, install a generated wheel offline, and read a local manual page. It reports `FocusVM guest verification passed.` on success.
