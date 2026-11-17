# Offline resources and synchronization

## Reference library

Use **Resources** in the manager to import, find by filename, open, and remove local references. The library accepts PDF, HTML, Markdown, ZIM, Git bundles, and Python wheels. Files are stored under `$XDG_DATA_HOME/focusvm/resources` and selected per disc. Removing an entry removes its library copy. The original file and any disc already made from the entry remain available.

```sh
focusvm resources add reference.pdf
focusvm resources search reference
focusvm resources list --json
focusvm resources build --output references.iso RESOURCE_ID
focusvm resources attach --vm VM_UUID RESOURCE_ID
focusvm resources eject --vm VM_UUID
```

Replace `RESOURCE_ID` and `VM_UUID` with the IDs from `focusvm resources list` and `focusvm list`. Pass multiple resource IDs after the options to select several files. Building a disc requires `xorriso`. Attach and eject require a stopped VM and an ended focus session. The guest sees a read-only disc named `FOCUSVM_DOCS` alongside the first-boot seed. Open it from the guest file manager. `index.txt` maps entries to their files. Replacing or ejecting a disc removes the previous FocusVM resource volume from the VM's libvirt pool once the VM no longer references it. If the pool refuses the removal, the old volume stays and the new disc still attaches.

The current guest recipe includes the GNU C++ standard library documentation. Run `man -s 3 std::vector` in the guest for a reference entry. To bring your own references, import a PDF, standalone HTML file, or ZIM archive. To keep the links and assets in a multi-file HTML book such as cppreference, package the unpacked book with `zimwriterfs` and import the resulting ZIM. [Fedora libstdc++ documentation](https://packages.fedoraproject.org/pkgs/gcc/libstdc%2B%2B-docs/) · [cppreference offline archives](https://en.cppreference.com/Cppreference%3AArchives) · [openZIM tools](https://github.com/openzim/zim-tools)

Open PDFs with Atril, HTML with Firefox, and Markdown with Vim. For an offline Wikipedia or other ZIM archive, run `kiwix-serve --address=127.0.0.1 --port=8080 /path/to/archive.zim` inside the guest and open `http://127.0.0.1:8080` there. The server uses the guest's loopback interface. Earlier guest images need the viewers and C++ documentation separately. Host ZIM viewing uses Kiwix Desktop when installed. [Kiwix archive catalog](https://browse.library.kiwix.org/) · [kiwix-serve manual](https://kiwix-tools.readthedocs.io/en/latest/kiwix-serve.html)

## Git

Create bundles from committed branch history. Copy a host bundle into the guest using the reference disc. In the guest, inspect it with `git bundle list-heads`, then fetch its branch into a review branch. Git credentials stay on the host. [Git bundle format and commands](https://git-scm.com/docs/git-bundle)

```sh
focusvm sync bundle --repo /path/to/host/repo --branch main --output host.bundle
focusvm resources add host.bundle
```

To export from the guest, run `git bundle create ~/guest.bundle refs/heads/main` in the guest repository, then shut down the VM. The host extraction command needs `guestfish` and read access to the managed overlay and its backing file. FocusVM opens the disk read-only and writes a new output file. [Guestfish read-only disk access](https://libguestfs.org/guestfish.1.html)

```sh
focusvm sync export-guest --vm VM_UUID --output guest.bundle /home/focusvm/guest.bundle
focusvm sync import --repo /path/to/host/repo --branch main guest.bundle
git -C /path/to/host/repo log refs/focusvm/bundles/main
```

Imports preserve a different commit already at the same review ref. After review, use normal Git commands to integrate it into a local branch. Fetch and push use the named remote configured in the specified host repository. Fetch writes `refs/focusvm/remotes/REMOTE/BRANCH` and leaves a divergent review ref unchanged. Push sends one explicitly named local branch without force. HTTPS and SSH remotes include GitHub repositories. The commands use the host's normal Git credentials and require you to end any focus session first. Run them in host repositories you trust.

```sh
focusvm sync fetch --repo /path/to/host/repo --remote origin --branch main
focusvm sync push --repo /path/to/host/repo --remote origin --branch main
```

## WakaTime activity

Activity is disabled until you enable specific project names. `record` queues an explicitly supplied file event with a timestamp. It can be called by a host editor integration or entered manually. The queue stores project names, project-relative file paths, and timestamps. This version has manual recording and no guest editor plugin.

```sh
focusvm tracking enable study
focusvm tracking record --project study --file src/main.cpp
focusvm tracking status
focusvm tracking sync --prompt-key
```

An upload requires an ended focus session and sends only events for currently enabled projects. Use `focusvm tracking store-key` to put a key in an unlocked desktop Secret Service keyring via `secret-tool`, then run `focusvm tracking sync` without the prompt. The key stays out of the queue and settings. Use `tracking forget-key` to remove it. `tracking disable` pauses recording and upload while preserving the queue. `tracking clear` discards queued events. An interrupted upload leaves any unconfirmed event queued for retry. [WakaTime heartbeat API](https://wakatime.com/developers#heartbeats)
