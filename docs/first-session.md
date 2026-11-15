# First FocusVM session

This walkthrough starts with a Linux host that has KVM and a signed guest image. Install the host tools first if needed. The first boot can take longer while the guest creates your login and expands its filesystem. [Host setup](setup.md#quick-install) · [Use a published guest](guest.md#use-a-published-guest) · [Build and sign a guest image](guest.md#sign-and-verify)

From the source checkout, run these commands as your desktop user.

```sh
./install.sh
focusvm setup
focusvm verify-image --image guest.qcow2 --manifest guest.json \
  --signature guest.sig --keyring trusted.gpg
focusvm create --name practice --image guest.qcow2 --manifest guest.json \
  --signature guest.sig --keyring trusted.gpg
focusvm session start --profile deep-work
```

Use `~/.local/bin/focusvm` if the new command is outside your shell's search path. Enter a guest password when prompted and sign into the guest as `focusvm`. From the desktop app, the equivalent actions are **Virtual Machines → Create VM** and **Focus Sessions → Start Focus Session**.

When the viewer is focused, press **Shift+F12** to release input and **Shift+F11** to toggle fullscreen. From a host terminal, end the session and shut the guest down when you finish.

```sh
focusvm session status
focusvm session end
focusvm stop
```

If setup fails, run `focusvm doctor` and check the matching symptom in the host guide. [Troubleshooting](setup.md#troubleshooting) · [Session recovery](sessions.md#start-end-and-recover)
