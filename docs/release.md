# Preparing and publishing a release

The first release candidate is `0.1.0rc1` with the Git tag `v0.1.0rc1`. This is a source-first preview for the host CLI and desktop manager. The Fedora guest is a separately versioned build. [Python package version specifiers](https://packaging.python.org/en/latest/specifications/version-specifiers/)

## Validate the release commit

1. Check that `focusvm --version` and the [changelog](../CHANGELOG.md) agree. Review the release changes and license files, then select the exact commit to tag.
2. Run the local lint, strict typing, test, and build checks. Record the desktop, KVM, and guest acceptance results for the selected revision. The [verification checklist](verification.md) defines the reference-host checks.
3. Create an annotated tag for the reviewed version and commit. For changes after a tag exists, prepare a new candidate version and tag.
4. Before publishing, confirm a successful GitHub Actions run for the exact commit. Include the reference checks and pending results in release notes. A general-use release needs both Fedora desktop logins and physical viewer controls verified. [Viewing GitHub Actions runs](https://docs.github.com/en/actions/how-tos/monitor-workflows/view-workflow-run-history) · [Manual acceptance record](verification.md#manual-acceptance-record)

## Build and check the Python artifacts

Use a development environment with the `build` package installed. Run these commands from the repository root after creating the candidate tag, with no older wheel or source archive in `dist`. Replace the example version with the candidate's version. The archive gives `build` the tracked files at that tag. Use the current checkout for the archive validator. [PyPA build](https://build.pypa.io/en/stable/)

```sh
set -eu
version=0.1.0rc1
stage=$(mktemp -d)
mkdir "$stage/source"
git archive --format=tar --output="$stage/source.tar" "v$version"
tar -xf "$stage/source.tar" -C "$stage/source"
python -m build --outdir "$PWD/dist" "$stage/source"
python -m scripts.check_distribution "$PWD/dist"
(
  cd dist
  sha256sum "focusvm-$version-py3-none-any.whl" "focusvm-$version.tar.gz" > SHA256SUMS
  sha256sum -c SHA256SUMS
)
```

Install each artifact in a fresh Python 3.12 or newer virtual environment. Include the CLI help and version, configuration creation and validation, and the empty inventory and session checks. For the host acceptance test, install on Fedora using the system interpreter and the distribution's libvirt bindings. Exercise the Qt manager and fullscreen viewer from the intended Wayland session. [Host setup](setup.md)

```sh
set -eu
version=0.1.0rc1
wheel=$(realpath "dist/focusvm-$version-py3-none-any.whl")
archive=$(realpath "dist/focusvm-$version.tar.gz")
for asset in "$wheel" "$archive"; do
  smoke=$(mktemp -d)
  python3.12 -m venv "$smoke/venv"
  "$smoke/venv/bin/python" -m pip install "$asset"
  (
    cd "$smoke"
    export XDG_CONFIG_HOME="$smoke/config" XDG_STATE_HOME="$smoke/state"
    export XDG_CACHE_HOME="$smoke/cache" XDG_DATA_HOME="$smoke/data"
    focusvm="$smoke/venv/bin/focusvm"
    "$focusvm" --help
    test "$("$focusvm" --version)" = "focusvm $version"
    "$focusvm" init --storage-pool focusvm-test
    "$focusvm" config validate
    test "$("$focusvm" list --json)" = '[]'
    test "$("$focusvm" session status --json)" = 'null'
  )
done
```

## Prepare the guest separately

Build and verify the guest from the exact source commit you plan to tag. The older `fedora-44.2` development image includes Fedora release and identity packages. Use the current [guest build recipe](guest.md#build-the-base-image) for a FocusVM-branded release candidate. Review the final image and release text for trademarked branding. If you plan to use the Fedora Remix marks, follow Fedora's requirements for replacing branded packages and displaying its support and attribution notices. [Fedora trademark guidelines](https://gitlab.com/fedora/legal/fedora-legal-docs/-/blob/main/modules/ROOT/pages/trademarks.adoc#distributing-fedora-materials)

Review the exact package inventory and retain the required licenses, notices, and redistribution conditions. Use the installed RPM versions and their source RPM metadata to identify the corresponding source, including patches and build scripts. Provide equivalent access alongside the guest binary download for packages whose licenses require it, and keep those sources available for as long as the image is offered. For GitHub, the guest release notes can point to a companion source archive or download location operated by the publisher. The inventory and build recipe alone are insufficient to provide corresponding source. [Fedora distribution guidance](https://fedoraproject.org/wiki/Legal:Distribution) · [GNU GPL FAQ](https://www.gnu.org/licenses/gpl-faq.html#SourceAndBinaryOnDifferentSites)

### Sign the manifest

Use a dedicated OpenPGP key controlled by the maintainer. If creating a key for the first guest release, replace the identity with the project's real maintainer contact and choose a passphrase when prompted.

```sh
gpg --quick-generate-key 'FocusVM Guest Publisher <maintainer@example.org>' ed25519 sign 2y
gpg --list-keys --with-fingerprint 'FocusVM Guest Publisher'
```

Keep the private key, backups, and revocation certificate under the maintainer's control outside the repository, CI, and release assets. Record the full primary key fingerprint in the release notes and make it independently verifiable through a trusted maintainer channel, such as the maintainer's established website or direct contact. Export a public copy for the download. [GnuPG key generation and management](https://gnupg.org/documentation/manuals/gnupg/OpenPGP-Key-Management.html)

### Stage the release assets

Run from the guest's source checkout in the FocusVM Python environment with GnuPG, `qemu-img`, and `zstd` installed. Allow temporary space for a copy of both the uncompressed and compressed image. Set `fingerprint` to the trusted full primary key fingerprint. Reuse `guest.sig` if the verified build was already signed with this key.

```sh
set -eu
version=guest-44.3
build="guest-output/$version"
fingerprint=YOUR_FULL_PUBLISHER_PRIMARY_KEY_FINGERPRINT
gpg --local-user "$fingerprint" --detach-sign --output "$build/guest.sig" "$build/guest.json"
gpg --export "$fingerprint" > "$build/publisher.gpg"
zstd -T0 -19 -o "$build/guest.qcow2.zst" -- "$build/guest.qcow2"
python -m scripts.prepare_guest_release \
  --build-dir "$build" --version "$version" \
  --compressed-image "$build/guest.qcow2.zst" \
  --signature "$build/guest.sig" --keyring "$build/publisher.gpg" \
  --publisher-fingerprint "$fingerprint" --output "dist/guest/$version"
```

The script verifies the publisher fingerprint and detached signature, the signed guest version, the QCOW2 and its digest, the decompressed upload, and the recorded build inputs against the current recipe. It stages the guest release files and generates `SHA256SUMS`. Each asset must be smaller than 2 GiB to stage it for GitHub. For a larger image, use another artifact host and manually generate the distribution checksums. Verify the uncompressed guest with `focusvm verify-image` and test a fresh download before publishing. [GitHub release asset limits](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases#storage-and-bandwidth-quotas)

Create a separate GitHub prerelease draft from the guest version's tag and attach the staged files. Record the source commit, installer digest, guest digest, publisher fingerprint, redistribution information, and verification results in its notes. Download the assets into a fresh directory and follow the [published guest instructions](guest.md#use-a-published-guest). Import the downloaded guest into a disposable VM and verify that it boots offline before publishing.

## Prepare the GitHub draft

After the repository owner authorizes remote changes, push the reviewed commit and annotated tag through the project's normal branch workflow. In GitHub Releases, create a draft from the exact tag, mark it as a prerelease, and copy the candidate's notes from the [changelog](../CHANGELOG.md). Attach the verified wheel, source distribution, and `SHA256SUMS`. Publish the approved guest separately when its signing and distribution requirements are met. [Managing GitHub releases](https://docs.github.com/en/repositories/releasing-projects-on-github/managing-releases-in-a-repository)

Before publishing, re-download the draft assets and run `sha256sum -c SHA256SUMS`. Verify the tag's commit, hosted CI result, release notes, and compatibility record against the local checks. Publish after the release checks pass.
