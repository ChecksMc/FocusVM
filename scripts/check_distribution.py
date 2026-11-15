"""Validate the contents of the Python release artifacts before installation."""

import argparse
import stat
from pathlib import Path, PurePosixPath
from tarfile import TarError
from tarfile import open as open_tar
from zipfile import BadZipFile, ZipFile

_SOURCE_REQUIRED = {
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "LICENSE",
    "NOTICE",
    "README.md",
    "SECURITY.md",
    "docs/setup.md",
    "guest/build.py",
    "guest/fedora-44.ks",
    "guest/verify.sh",
    "install.sh",
    "scripts/install.py",
    "scripts/prepare_guest_release.py",
    "scripts/setup_fedora.py",
    "setup-fedora.sh",
    "src/focusvm/cli.py",
    "uninstall.sh",
}
_SOURCE_ROOT_FILES = _SOURCE_REQUIRED | {
    "MANIFEST.in",
    "PKG-INFO",
    "pyproject.toml",
    "setup.cfg",
}
_SOURCE_METADATA = {
    "PKG-INFO",
    "SOURCES.txt",
    "dependency_links.txt",
    "entry_points.txt",
    "requires.txt",
    "top_level.txt",
}
_WHEEL_METADATA = {
    "METADATA",
    "RECORD",
    "WHEEL",
    "entry_points.txt",
    "licenses/LICENSE",
    "licenses/NOTICE",
    "top_level.txt",
}


def check_distribution(directory: Path) -> str:
    """Require matching FocusVM archives and return their version."""
    archives = list(directory.glob("*.tar.gz"))
    wheels = list(directory.glob("*.whl"))
    if len(archives) != 1 or len(wheels) != 1:
        raise ValueError("Expected exactly one source archive and one wheel")
    source, wheel = archives[0], wheels[0]
    release = source.name.removesuffix(".tar.gz")
    if (
        not release.startswith("focusvm-")
        or wheel.name != f"{release}-py3-none-any.whl"
    ):
        raise ValueError(
            "Expected a FocusVM source archive and matching pure-Python wheel"
        )

    source_files: set[str] = set()
    source_dirs: set[str] = set()
    with open_tar(source, "r:gz") as archive:
        for member in archive:
            path = _safe_member(member.name)
            if not path.is_relative_to(release) or not (
                member.isfile() or member.isdir()
            ):
                raise ValueError(f"Unexpected source archive member: {member.name}")
            relative = path.relative_to(release).as_posix()
            if relative in source_files or relative in source_dirs:
                raise ValueError(f"Duplicate source archive member: {member.name}")
            (source_dirs if member.isdir() else source_files).add(relative)
    _require_files(source_files, _SOURCE_REQUIRED, "source archive")
    for item in sorted(source_files):
        path = PurePosixPath(item)
        if item not in _SOURCE_ROOT_FILES and not _source_member_allowed(path):
            raise ValueError(f"Unexpected source archive file: {item}")
    _require_directories(source_dirs, source_files, "source archive")

    metadata = f"{release}.dist-info"
    package = {
        item.removeprefix("src/")
        for item in source_files
        if item.startswith("src/focusvm/")
    }
    expected = package | {f"{metadata}/{item}" for item in _WHEEL_METADATA}
    wheel_files: set[str] = set()
    wheel_dirs: set[str] = set()
    with ZipFile(wheel) as zip_archive:
        for entry in zip_archive.infolist():
            name = _safe_member(entry.filename).as_posix()
            kind = stat.S_IFMT(entry.external_attr >> 16)
            if kind not in (0, stat.S_IFDIR if entry.is_dir() else stat.S_IFREG):
                raise ValueError(f"Unexpected wheel member: {entry.filename}")
            if name in wheel_files or name in wheel_dirs:
                raise ValueError(f"Duplicate wheel member: {entry.filename}")
            (wheel_dirs if entry.is_dir() else wheel_files).add(name)
    _require_files(wheel_files, expected, "wheel")
    if unexpected := wheel_files - expected:
        raise ValueError(f"Unexpected wheel files: {', '.join(sorted(unexpected))}")
    _require_directories(wheel_dirs, wheel_files, "wheel")
    return release.removeprefix("focusvm-")


def _safe_member(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if (
        path.is_absolute()
        or "\\" in name
        or any(
            not part or part.startswith(".") or part in {"__pycache__", "analysis.md"}
            for part in name.rstrip("/").split("/")
        )
    ):
        raise ValueError(f"Unexpected archive path: {name}")
    return path


def _source_member_allowed(path: PurePosixPath) -> bool:
    if path.is_relative_to("src/focusvm"):
        return path.suffix == ".py"
    if path.is_relative_to("src/focusvm.egg-info"):
        return path.relative_to("src/focusvm.egg-info").as_posix() in _SOURCE_METADATA
    suffixes = {
        "docs": {".md", ".png"},
        "guest": {".py", ".ks", ".sh"},
        "scripts": {".py"},
        "tests": {".py"},
    }
    return len(path.parts) >= 2 and path.suffix in suffixes.get(path.parts[0], set())


def _require_files(actual: set[str], expected: set[str], archive: str) -> None:
    if missing := expected - actual:
        raise ValueError(
            f"The {archive} is missing files: {', '.join(sorted(missing))}"
        )


def _require_directories(dirs: set[str], files: set[str], archive: str) -> None:
    parents = {str(parent) for name in files for parent in PurePosixPath(name).parents}
    if unexpected := dirs - parents:
        raise ValueError(
            f"Unexpected {archive} directories: {', '.join(sorted(unexpected))}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "directory", type=Path, help="directory containing the wheel and sdist"
    )
    args = parser.parse_args()
    try:
        version = check_distribution(args.directory)
    except (ValueError, OSError, TarError, BadZipFile) as error:
        parser.exit(1, f"{error}\n")
    print(f"Verified FocusVM {version} distribution contents.")


if __name__ == "__main__":
    main()
