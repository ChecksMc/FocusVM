# Advanced sessions and maintenance

## Profiles and contest timer

Choose **Deep Work**, **Contest**, or **Research** on the manager's **Focus Sessions** page. Deep Work and Research default to Balanced. Contest defaults to Strict and can display an optional countdown. Both network policies keep the guest offline. Set `application.default_profile` in `config.toml` to choose your default profile.

```sh
focusvm profiles list
focusvm session start --profile contest --duration-minutes 45
focusvm session status
focusvm session end
```

The countdown uses the original start time after a viewer closes or the manager restarts. At zero, the manager and CLI report that the timer expired. The guest and viewer stay available for saving work. For Research, attach PDFs, ZIM archives, notes, or saved references before boot. [Offline reference library](resources-and-sync.md#reference-library)

## Approved online references

Use the host to fetch a particular public HTTPS reference outside a focus session. Approve the exact hostname first. Each save writes one Markdown file containing the page's plain text. HTML markup, scripts, and linked assets are excluded. Set an explicit output name and import the file into the reference library before starting Research.

```sh
focusvm research allow docs.python.org
focusvm research hosts
focusvm research save https://docs.python.org/3/library/pathlib.html --output pathlib.md
focusvm resources add pathlib.md
focusvm research revoke docs.python.org
```

The allowlist starts empty. It applies to `research save`, which connects only to a public IPv4 address for the approved hostname, requires normal TLS verification, and stops at redirects. Provide the final HTTPS URL when a server redirects. Supported inputs are HTML, Markdown, and plain text up to 2 MiB. The guest keeps its offline network configuration throughout. Flexible guest networking is currently unavailable.

## Offline Python packages

Collect trusted wheel files for the guest's Python version and architecture, including transitive dependencies. Attach them to a stopped VM outside a focus session. The wheel disc uses the reference disc slot. `packages build --output wheels.iso` writes the same read-only media to a host file for inspection.

```sh
focusvm packages attach --vm VM_UUID ./wheelhouse/*.whl
focusvm start --vm VM_UUID
```

Open the disc in the guest file manager. In the project directory, run the following commands, adjusting the mount path if necessary. The install takes its inputs from the selected wheels. `pip check` reports any missing or incompatible dependencies. [pip install options](https://pip.pypa.io/en/stable/cli/pip_install/) · [pip configuration](https://pip.pypa.io/en/stable/topics/configuration/#pip-config-file)

```sh
python3 -m venv .venv
PIP_CONFIG_FILE=/dev/null .venv/bin/python -m pip --isolated --disable-pip-version-check \
  install --no-index --only-binary=:all: --no-deps /run/media/focusvm/FOCUSVM_DOCS/*/*.whl
.venv/bin/python -m pip check
```

## Optional Cage viewer

Install Cage on a local Wayland desktop, then select **Use Cage on Wayland** or start with `focusvm session start --cage`. Cage runs as a nested host window. Use the host window's close action, the manager, or `focusvm session end` from another host terminal to leave. Closing the window leaves the session ready to reopen with the same viewer choice. The host compositor still controls its own shortcuts. [Cage project](https://github.com/cage-kiosk/cage)

## Journal and statistics

Journal entries are recorded manually. They can include completed tasks, problems, items solved independently, references, and notes. `journal stats` summarizes the session count and recorded minutes, overall and by project. Entries stay in the local state directory.

```sh
focusvm journal record --project practice --minutes 45 \
  --completed 'Implemented a heap' --solved 'Fixed a boundary case'
focusvm journal list --project practice
focusvm journal stats --project practice
focusvm journal remove ENTRY_ID
```
