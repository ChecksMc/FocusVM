"""Explicit Git bundle transfer and single-branch synchronization."""

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import BinaryIO, Literal
from urllib.parse import urlsplit
from uuid import uuid4

type Direction = Literal["fetch", "push"]

_REPOSITORY_ENVIRONMENT = {
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_CONFIG",
    "GIT_CONFIG_COUNT",
    "GIT_CONFIG_PARAMETERS",
    "GIT_DIR",
    "GIT_GRAFT_FILE",
    "GIT_IMPLICIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_PREFIX",
    "GIT_REPLACE_REF_BASE",
    "GIT_SHALLOW_FILE",
    "GIT_WORK_TREE",
}
_OBJECT_ID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
_REMOTE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_SCP_URL = re.compile(r"(?:[^/@:\s]+@)?(?:[A-Za-z0-9_.-]+|\[[0-9A-Fa-f:]+\]):.+")


class GitSyncInputError(ValueError):
    """The selected repo, branch, remote, or bundle cannot be used."""


class GitSyncError(RuntimeError):
    """Git could not complete the selected operation."""


def create_bundle(repo: Path, branch: str, output: Path) -> None:
    """Export committed history for one branch into a new bundle file."""
    git = _Repository(repo)
    source = git.branch_ref(branch)
    if git.read_ref(source) is None:
        raise GitSyncInputError(f"Local branch {branch!r} does not exist")
    output = output.absolute()
    if os.path.lexists(output):
        raise GitSyncInputError(f"Bundle output already exists: {output}")
    try:
        with TemporaryDirectory(
            prefix=".focusvm-bundle-", dir=output.parent
        ) as staging:
            staged = Path(staging) / "branch.bundle"
            if git.run("bundle", "create", str(staged), source).returncode:
                raise GitSyncError(f"Git could not bundle branch {branch!r}")
            staged.chmod(0o600)
            with staged.open("rb") as stream:
                os.fsync(stream.fileno())
            os.link(staged, output)
    except FileExistsError as error:
        raise GitSyncInputError(f"Bundle output already exists: {output}") from error
    except OSError as error:
        raise GitSyncError(
            f"Could not publish bundle at {output}: {error.strerror}"
        ) from error


def import_bundle(repo: Path, bundle: Path, branch: str) -> str:
    """Import one bundle ref under refs/focusvm/bundles for manual review."""
    git = _Repository(repo)
    source = git.branch_ref(branch)
    destination = f"refs/focusvm/bundles/{branch}"
    try:
        with open(bundle, "rb", opener=_open_regular) as stream:
            result = git.run("bundle", "list-heads", "-", source, input_stream=stream)
            if result.returncode:
                raise GitSyncInputError(f"Invalid Git bundle: {bundle}")
            commit = _bundle_commit(result.stdout, source)
            existing = git.read_ref(destination)
            if existing is not None and existing != commit:
                raise GitSyncInputError(
                    f"Ref {destination} already holds a different commit"
                )

            stream.seek(0)
            result = git.run("bundle", "unbundle", "-", source, input_stream=stream)
            if result.returncode or _bundle_commit(result.stdout, source) != commit:
                raise GitSyncError(
                    "Git could not import the bundle or its prerequisites are missing"
                )
    except OSError as error:
        raise GitSyncInputError(
            f"Cannot read Git bundle {bundle}: {error.strerror}"
        ) from error

    git.publish(destination, commit, existing)
    return destination


def sync_remote(repo: Path, remote: str, branch: str, direction: Direction) -> str:
    """Fetch into a review ref or push exactly one local branch without force."""
    git = _Repository(repo)
    branch_ref = git.branch_ref(branch)
    if direction not in {"fetch", "push"}:
        raise GitSyncInputError(f"Unknown Git sync direction: {direction!r}")
    alias, options, protocol = git.remote_options(remote, direction)

    if direction == "push":
        if git.read_ref(branch_ref) is None:
            raise GitSyncInputError(f"Local branch {branch!r} does not exist")
        result = git.run(
            *options,
            "-c",
            "push.pushOption=",
            "push",
            "--no-force",
            "--no-follow-tags",
            "--recurse-submodules=no",
            "--",
            alias,
            f"{branch_ref}:{branch_ref}",
            protocol=protocol,
        )
        if result.returncode:
            raise GitSyncError(
                f"Git could not push {branch!r} to {remote!r}. Check access and whether the push is a fast-forward."
            )
        return branch_ref

    destination = f"refs/focusvm/remotes/{remote}/{branch}"
    current = git.read_ref(destination)
    staged = f"refs/focusvm/staging/{uuid4().hex}"
    try:
        result = git.run(
            *options,
            "fetch",
            "--refmap=",
            "--no-tags",
            "--no-prune",
            "--no-prune-tags",
            "--no-recurse-submodules",
            "--no-write-fetch-head",
            "--no-auto-maintenance",
            "--no-write-commit-graph",
            "--",
            alias,
            f"{branch_ref}:{staged}",
            protocol=protocol,
        )
        incoming = git.read_ref(staged)
        if result.returncode or incoming is None:
            raise GitSyncError(
                f"Git could not fetch {branch!r} from {remote!r}. Check access and the branch name."
            )
        if current is not None and current != incoming:
            ancestry = git.run("merge-base", "--is-ancestor", current, incoming)
            if ancestry.returncode == 1:
                raise GitSyncError(
                    f"Fetched history diverged from {destination}. Review it before retrying."
                )
            if ancestry.returncode:
                raise GitSyncError(
                    f"Could not check fetched history against {destination}"
                )
        git.publish(destination, incoming, current)
    finally:
        git.run("update-ref", "--no-deref", "-d", staged)
    return destination


class _Repository:
    def __init__(self, path: Path) -> None:
        if (git := shutil.which("git")) is None:
            raise GitSyncError("Install Git to transfer or synchronize a repository")
        self.git = git
        self.path = path.resolve()
        self.environment = {
            key: value
            for key, value in os.environ.items()
            if key not in _REPOSITORY_ENVIRONMENT
        }
        self.environment.update(
            {"GIT_NO_LAZY_FETCH": "1", "GIT_NO_REPLACE_OBJECTS": "1"}
        )
        if not self.path.is_dir():
            raise GitSyncInputError(f"Expected a Git repository directory: {path}")
        root = self.run("rev-parse", "--show-toplevel")
        if root.returncode == 0 and Path(root.stdout.removesuffix("\n")) == self.path:
            return
        bare = self.run("rev-parse", "--is-bare-repository", "--absolute-git-dir")
        if bare.returncode == 0 and bare.stdout == f"true\n{self.path}\n":
            return
        raise GitSyncInputError(
            f"Choose the root directory of a Git repository: {path}"
        )

    def run(
        self, *args: str, input_stream: BinaryIO | None = None, protocol: str = ""
    ) -> subprocess.CompletedProcess[str]:
        environment = {**self.environment, "GIT_ALLOW_PROTOCOL": protocol}
        try:
            return subprocess.run(  # noqa: S603 - fixed executable and separate arguments
                [self.git, *args],
                cwd=self.path,
                env=environment,
                stdin=input_stream
                if input_stream is not None
                else None
                if protocol
                else subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="surrogateescape",
                check=False,
            )
        except OSError as error:
            raise GitSyncError(f"Could not run Git: {error.strerror}") from error

    def branch_ref(self, branch: str) -> str:
        ref = f"refs/heads/{branch}"
        if (
            not branch
            or branch.startswith("-")
            or branch == "HEAD"
            or "\0" in branch
            or self.run("check-ref-format", ref).returncode
        ):
            raise GitSyncInputError(f"Invalid Git branch name: {branch!r}")
        return ref

    def read_ref(self, ref: str) -> str | None:
        if self.run("symbolic-ref", "--quiet", ref).returncode == 0:
            raise GitSyncInputError(f"Ref {ref} is symbolic")
        result = self.run("rev-parse", "--verify", "--quiet", "--end-of-options", ref)
        commit = result.stdout.strip()
        if result.returncode == 1:
            return None
        if result.returncode or _OBJECT_ID.fullmatch(commit) is None:
            raise GitSyncError(f"Could not inspect Git ref {ref}")
        return commit

    def publish(self, ref: str, commit: str, expected: str | None) -> None:
        if self.run("cat-file", "-t", commit).stdout.strip() != "commit":
            raise GitSyncError(f"Git ref {ref} does not point to a commit")
        if expected == commit:
            return
        result = self.run(
            "update-ref", "--no-deref", ref, commit, expected or "0" * len(commit)
        )
        if result.returncode and self.read_ref(ref) != commit:
            raise GitSyncError(
                f"Could not publish {ref}. It may have changed concurrently."
            )

    def remote_options(
        self, name: str, direction: Direction
    ) -> tuple[str, list[str], str]:
        if _REMOTE_NAME.fullmatch(name) is None:
            raise GitSyncInputError(f"Invalid configured Git remote name: {name!r}")

        # An invocation-only remote preserves Git's URL rewriting and credentials while
        # ignoring the selected remote's extra refspecs, mirror setting, and hooks options.
        alias = f"focusvm-{uuid4().hex}"
        url = self._remote_value(name, "url")
        if url is None:
            raise GitSyncInputError(f"No URL is configured for Git remote {name!r}")
        options = ["-c", f"remote.{alias}.url={url}"]
        if (
            direction == "push"
            and (push_url := self._remote_value(name, "pushurl")) is not None
        ):
            options.extend(["-c", f"remote.{alias}.pushurl={push_url}"])
        push = ["--push"] if direction == "push" else []
        result = self.run("remote", "get-url", "--all", *push, name)
        urls = result.stdout.splitlines()
        if result.returncode or len(urls) != 1:
            raise GitSyncInputError(f"Git remote {name!r} must resolve to one URL")
        protocol = _remote_protocol(urls[0])
        return alias, options, protocol

    def _remote_value(self, remote: str, key: str) -> str | None:
        result = self.run("config", "--null", "--get-all", f"remote.{remote}.{key}")
        if result.returncode == 1:
            return None
        values = result.stdout.split("\0")
        if result.returncode or len(values) != 2 or not values[0].isprintable():
            raise GitSyncInputError(
                f"Git remote {remote!r} must configure one valid {key}"
            )
        if values[0].startswith("https://") and "@" in values[0].split("/", 3)[2]:
            raise GitSyncInputError(
                "Use Git credentials helpers instead of credentials in remote URLs"
            )
        return values[0]


def _remote_protocol(url: str) -> str:
    if not url.isprintable() or url.startswith("-") or "::" in url.split("://", 1)[0]:
        raise GitSyncInputError("Unsupported Git remote URL")
    if "://" in url:
        try:
            parsed = urlsplit(url)
            if (
                parsed.scheme in {"https", "ssh"}
                and parsed.hostname
                and parsed.password is None
            ):
                if parsed.scheme == "ssh" or parsed.username is None:
                    return parsed.scheme
            if (
                parsed.scheme == "file"
                and not parsed.netloc
                and parsed.path.startswith("/")
            ):
                return "file"
        except ValueError as error:
            raise GitSyncInputError("Invalid Git remote URL") from error
        raise GitSyncInputError(
            "Use an SSH, HTTPS, or local Git remote URL without embedded credentials"
        )
    if _SCP_URL.fullmatch(url):
        return "ssh"
    if url.startswith(("/", "./", "../")):
        return "file"
    raise GitSyncInputError("Use an SSH, HTTPS, or explicit local Git remote URL")


def _bundle_commit(output: str, ref: str) -> str:
    commit, _, advertised_ref = output.rstrip("\n").partition(" ")
    if advertised_ref != ref or _OBJECT_ID.fullmatch(commit) is None:
        raise GitSyncInputError(
            f"The bundle does not contain branch {ref.removeprefix('refs/heads/')!r}"
        )
    return commit


def _open_regular(path: str, flags: int) -> int:
    descriptor = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise GitSyncInputError(f"Expected a regular Git bundle file: {path}")
    except (OSError, GitSyncInputError):
        os.close(descriptor)
        raise
    return descriptor
