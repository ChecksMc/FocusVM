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

The Fedora `fedora-44.2` development build is unsigned. Use the [guest build guide](guest.md) to build and verify a local guest. Before redistributing any guest version, record the source and license obligations for its package set, confirm the applicable Fedora trademark terms, and retain the package inventory and build inputs. Sign `guest.json` using the publisher's OpenPGP key. Publish verified checksums for the exact assets, and provide the public key or its fingerprint through an independently trusted channel. Record the source commit, installer digest, guest digest, key fingerprint, and verification results together. [Fedora distribution guidance](https://fedoraproject.org/wiki/Distribution)

Keep the uncompressed `guest.qcow2` digest in `guest.json`. If the image is distributed as a compressed archive, record a separate checksum for the download and decompress it before importing. Check that the decompressed image matches the signed manifest and that a disposable VM can import and boot it. GitHub requires individual release assets to be smaller than 2 GiB. Use a compressed file within that limit or host the guest separately. [GitHub release asset limits](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases#storage-and-bandwidth-quotas)

## Prepare the GitHub draft

After the repository owner authorizes remote changes, push the reviewed commit and annotated tag through the project's normal branch workflow. In GitHub Releases, create a draft from the exact tag, mark it as a prerelease, and copy the candidate's notes from the [changelog](../CHANGELOG.md). Attach the verified wheel, source distribution, and `SHA256SUMS`. Publish the approved guest separately when its signing and distribution requirements are met. [Managing GitHub releases](https://docs.github.com/en/repositories/releasing-projects-on-github/managing-releases-in-a-repository)

Before publishing, re-download the draft assets and run `sha256sum -c SHA256SUMS`. Verify the tag's commit, hosted CI result, release notes, and compatibility record against the local checks. Publish after the release checks pass.
