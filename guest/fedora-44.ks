#version=F44
# Install only in the disposable BIOS build VM with a blank virtio disk.
url --url=https://dl.fedoraproject.org/pub/fedora/linux/releases/44/Everything/x86_64/os/
network --bootproto=dhcp --device=link --activate --onboot=off --hostname=focusvm
lang en_US.UTF-8
keyboard --vckeymap=us --xlayouts=us
timezone Etc/UTC --utc
timesource --ntp-disable
rootpw --lock
firstboot --disable
selinux --enforcing
firewall --enabled

ignoredisk --only-use=vda
clearpart --all --initlabel --disklabel=gpt --drives=vda
bootloader --location=mbr --boot-drive=vda --append="console=tty0 console=ttyS0,115200n8 systemd.ssh_auto=no"
part biosboot --fstype=biosboot --ondisk=vda --size=2
part /boot --fstype=ext4 --ondisk=vda --size=2048
part / --fstype=ext4 --ondisk=vda --size=4096 --grow
poweroff

%packages --exclude-weakdeps
@core
generic-release
generic-logos
-fedora-release*
-fedora-logos*
kernel
NetworkManager
cloud-init
cloud-utils-growpart
e2fsprogs
lightdm
lightdm-gtk
xorg-x11-server-Xorg
xorg-x11-drv-libinput
mesa-dri-drivers
dbus-x11
dejavu-sans-fonts
dejavu-sans-mono-fonts
xfce4-appfinder
xfce4-panel
xfce4-session
xfce4-settings
xfce4-terminal
xfdesktop
xfwm4
Thunar
atril
firefox
kiwix-tools
vim-enhanced
git
gcc
gcc-c++
make
gdb
libstdc++-devel
libstdc++-docs
python3
python3-pip
python-pip-wheel
iproute
less
man-db
man-pages
procps-ng
util-linux
%end

%post --erroronfail --interpreter=/usr/bin/bash --log=/root/focusvm-install.log
set -euo pipefail

rpm -q generic-release generic-logos
if [[ -n $(rpm -qa 'fedora-release*' 'fedora-logos*') ]]; then
    printf 'The guest contains Fedora release or logo RPMs.\n' >&2
    exit 1
fi

# Give the guest its own identity while retaining Fedora compatibility metadata.
rm -f /etc/os-release /etc/issue /etc/issue.net
cat > /etc/os-release <<'EOF'
NAME="FocusVM Guest"
PRETTY_NAME="FocusVM Guest"
ID=focusvm
ID_LIKE=fedora
VERSION_ID=44
HOME_URL="https://github.com/ChecksMc/FocusVM"
SUPPORT_URL="https://github.com/ChecksMc/FocusVM/issues"
EOF
printf 'FocusVM Guest\n' > /etc/issue
cp /etc/issue /etc/issue.net

cat > /etc/cloud/cloud.cfg.d/99-focusvm.cfg <<'EOF'
datasource_list: [NoCloud]
network:
  config: disabled
disable_network_activation: true
growpart:
  mode: auto
  devices: ['/']
resize_rootfs: true
ssh_genkeytypes: []
ssh_publish_hostkeys:
  enabled: false
EOF

mkdir -p /etc/lightdm/lightdm.conf.d
cat > /etc/lightdm/lightdm.conf.d/99-focusvm.conf <<'EOF'
[Seat:*]
greeter-session=lightdm-gtk-greeter
user-session=xfce
EOF

systemctl enable cloud-init-main.service cloud-init-local.service \
    cloud-init-network.service cloud-config.service cloud-final.service lightdm.service
systemctl set-default graphical.target
systemctl mask sshd.service sshd.socket sshd-unix-local.socket sshd-vsock.socket
cloud-init clean --logs --machine-id --seed

test -s /usr/share/xgreeters/lightdm-gtk-greeter.desktop
test -s /usr/share/xsessions/xfce.desktop
test -s /etc/cloud/cloud.cfg.d/99-focusvm.cfg
for program in vim git gcc g++ make gdb python3 man xfce4-terminal atril firefox kiwix-serve; do
    command -v "$program"
done
rpm -q libstdc++-docs
python3 -m pip --version
temporary_venv=$(mktemp -d)
trap 'rm -rf -- "$temporary_venv"' EXIT
python3 -m venv "$temporary_venv"
"$temporary_venv/bin/python" -m pip --version

mkdir -p /usr/share/focusvm
rpm -qa --queryformat '%{NAME}-%{VERSION}-%{RELEASE}.%{ARCH}\n' |
    LC_ALL=C sort > /usr/share/focusvm/packages.txt
%end

%post --nochroot --erroronfail --interpreter=/usr/bin/bash
set -euo pipefail

inventory=/mnt/sysroot/usr/share/focusvm/packages.txt
test -s "$inventory"
{
    while IFS= read -r package; do
        printf 'FOCUSVM_RPM %s\n' "$package"
    done < "$inventory"
    printf 'FOCUSVM_INSTALL_COMPLETE\n'
} > /dev/ttyS0
%end

%onerror --interpreter=/usr/bin/bash
printf 'FOCUSVM_INSTALL_FAILED\n' > /dev/ttyS0
%end
