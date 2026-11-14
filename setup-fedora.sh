#!/bin/sh
set -eu

case "${1-}" in
    -h|--help)
        printf '%s\n' 'Usage: ./setup-fedora.sh' 'Install Fedora host tools and prepare the dedicated FocusVM storage pool.'
        exit 0
        ;;
esac
if [ "$#" -ne 0 ]; then
    printf '%s\n' 'Run ./setup-fedora.sh --help for usage.' >&2
    exit 2
fi

if [ ! -r /etc/os-release ] || ! ( . /etc/os-release && [ "${ID-}" = fedora ] ); then
    printf '%s\n' 'This helper is for Fedora. See docs/setup.md for manual setup.' >&2
    exit 2
fi
if [ -e /run/ostree-booted ]; then
    printf '%s\n' 'On an image-based Fedora host, install the prerequisites using the host package manager. See docs/setup.md.' >&2
    exit 2
fi

source_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
printf '%s\n' 'Preparing Fedora virtualization packages, local libvirt services, and the focusvm pool.'
sudo dnf install qemu-kvm qemu-img qemu-ui-spice-core libvirt-daemon-kvm \
    libvirt-daemon-driver-storage-core libvirt-client python3-libvirt \
    virt-viewer python3 python3-pip gnupg2-verify openssl xorriso policycoreutils
exec sudo /usr/bin/python3 -I "$source_dir/scripts/setup_fedora.py"
