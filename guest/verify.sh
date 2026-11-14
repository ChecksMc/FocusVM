#!/usr/bin/env bash
# Run as focusvm from an Xfce terminal, or use --serial for a boot smoke test.
set -Eeuo pipefail
export LC_ALL=C
umask 077

step='arguments'
trap 'printf "FAIL [%s] at line %s\n" "$step" "$LINENO" >&2' ERR

fail() {
    printf 'FAIL [%s] %s\n' "$step" "$*" >&2
    exit 1
}

serial=false
if [[ $# -eq 1 && $1 == --serial ]]; then
    serial=true
elif [[ $# -ne 0 ]]; then
    fail 'Usage: bash verify.sh [--serial]'
fi

step='guest identity'
[[ $EUID -ne 0 && $(id -un) == focusvm ]] || fail 'Sign in to the guest as focusvm and run without sudo.'
# shellcheck source=/dev/null
source /etc/os-release
[[ ${ID:-} == fedora && ${VERSION_ID:-} == 44 && $(uname -m) == x86_64 ]] || fail 'Expected Fedora 44 on x86_64.'

step='installed tools'
for tool in cloud-init timeout systemctl xfce4-session xfce4-terminal pgrep vim git g++ make gdb python3 man ip; do
    command -v "$tool" >/dev/null || fail "Missing command: $tool"
done

step='offline network'
for device in /sys/class/net/*; do
    [[ ${device##*/} == lo ]] || fail "Unexpected network interface: ${device##*/}"
done
ipv4_route=$(ip -4 route show default)
ipv6_route=$(ip -6 route show default)
[[ -z $ipv4_route && -z $ipv6_route ]] || fail 'Unexpected default network route.'
printf 'PASS offline network\n'

step='SSH disabled'
for unit in sshd.service sshd.socket sshd-unix-local.socket sshd-vsock.socket; do
    if systemctl is-active --quiet "$unit"; then
        fail "Unexpected active SSH unit: $unit"
    fi
done
printf 'PASS SSH disabled\n'

step='NoCloud first boot'
if ! $serial; then
    status=$(timeout 120 cloud-init status --wait --format json) || {
        printf '%s\n' "$status" >&2
        fail 'cloud-init did not complete cleanly. Inspect cloud-init status --long.'
    }
    python3 -c '
import json
import sys

status = json.load(sys.stdin)
if status.get("extended_status") != "done":
    sys.exit("Expected cloud-init extended_status=done")
' <<<"$status"
fi
cloud_name=$(cloud-init query v1.platform)
instance_id=$(cloud-init query v1.instance_id)
[[ $cloud_name == nocloud ]] || fail "Expected the NoCloud datasource, got: $cloud_name"
[[ $instance_id =~ ^[[:xdigit:]]{8}(-[[:xdigit:]]{4}){3}-[[:xdigit:]]{12}$ ]] || fail 'Expected the NoCloud instance ID to be a UUID.'
if $serial; then
    printf 'PASS NoCloud instance data\n'
else
    printf 'PASS NoCloud first boot\n'
fi

step='Xfce desktop'
[[ $(systemctl get-default) == graphical.target ]] || fail 'Expected graphical.target as the default boot target.'
[[ $(systemctl show --property=LoadState --value display-manager.service) == loaded ]] || fail 'No display manager service is configured.'
if $serial; then
    for (( attempt=0; attempt<30; attempt++ )); do
        if systemctl is-active --quiet display-manager.service; then break; fi
        sleep 1
    done
    systemctl is-active --quiet display-manager.service || fail 'The display manager did not start.'
    printf 'PASS Xfce installed and display manager active\n'
    root_size=$(df -B1 --output=size / | tail -n 1)
    (( root_size > 11 * 1024 * 1024 * 1024 )) || fail 'Root filesystem did not expand on the 14 GiB verification overlay.'
    printf 'PASS root filesystem expansion\n'
else
    [[ -n ${DISPLAY:-}${WAYLAND_DISPLAY:-} ]] && pgrep -u "$EUID" -x xfce4-session >/dev/null || fail 'Open Xfce Terminal in the guest desktop and run again.'
    xfce4-terminal --version
    printf 'PASS Xfce desktop\n'
fi

step='scratch directory'
workdir=$(mktemp -d)
trap 'rm -rf -- "$workdir"' EXIT
cd -- "$workdir"
mkdir home

step='Vim, C++, Make, and GDB'
cat > hello.cpp <<'CPP'
#include <iostream>

int main() {
    std::cout << 41 << '\n';
}
CPP
vim -Nu NONE -n -i NONE -es -c '%s/41/42/' -c wq hello.cpp
cat > Makefile <<'MAKE'
hello: hello.cpp
	g++ -std=c++17 -Wall -Wextra -g -o hello hello.cpp
MAKE
make
[[ $(./hello) == 42 ]] || fail 'Compiled sample did not print 42.'
DEBUGINFOD_URLS= gdb -nx -batch -return-child-result -ex 'set disable-randomization off' -ex 'break main' -ex run -ex continue ./hello
printf 'PASS Vim, C++, Make, and GDB\n'

step='local Git commit'
local_git() {
    env -i PATH="$PATH" HOME="$workdir/home" LC_ALL=C GIT_CONFIG_NOSYSTEM=1 git "$@"
}
local_git init --quiet --template= .
local_git add hello.cpp Makefile
local_git -c user.name='FocusVM verification' -c user.email='focusvm@example.invalid' \
    commit --quiet -m 'Verify offline Git'
local_git --no-pager log -1 --format='%h %s'
printf 'PASS local Git commit\n'

step='Python venv and offline pip'
python3 -m venv venv
venv/bin/python - <<'PY'
from zipfile import ZipFile

dist_info = "focusvm_verify_demo-0.0.0.dist-info"
files = {
    "focusvm_verify_demo.py": "value = 42\n",
    f"{dist_info}/METADATA": "Metadata-Version: 2.1\nName: focusvm-verify-demo\nVersion: 0.0.0\n",
    f"{dist_info}/WHEEL": "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
}
record = f"{dist_info}/RECORD"
files[record] = "".join(f"{name},,\n" for name in (*files, record))
with ZipFile("focusvm_verify_demo-0.0.0-py3-none-any.whl", "w") as wheel:
    for name, content in files.items():
        wheel.writestr(name, content)
PY
PIP_CONFIG_FILE=/dev/null venv/bin/python -m pip --isolated --disable-pip-version-check \
    install --no-index --no-deps --no-cache-dir ./focusvm_verify_demo-0.0.0-py3-none-any.whl
venv/bin/python -I -c 'import focusvm_verify_demo; assert focusvm_verify_demo.value == 42'
printf 'PASS Python venv and offline pip\n'

step='local manual page'
MANPAGER=cat man 2 open > open-man.txt
[[ -s open-man.txt ]] || fail 'The Linux open(2) manual page is missing or empty.'
printf 'PASS local manual page\n'

step='offline references'
for program in atril firefox kiwix-serve; do
    command -v "$program" > /dev/null
done
MANPAGER=cat man -s 3 std::vector > vector-man.txt
[[ -s vector-man.txt ]] || fail 'The C++ std::vector manual page is missing or empty.'
printf 'PASS offline references\n'

printf 'FocusVM guest verification passed.\n'
