"""Regression checks for data that must stay out of release archives."""

# pyright: reportPrivateUsage=false

from io import BytesIO
from pathlib import Path
from tarfile import SYMTYPE, TarInfo
from tarfile import open as open_tar
from zipfile import ZipFile

import pytest

from scripts.check_distribution import (
    _SOURCE_REQUIRED,
    _WHEEL_METADATA,
    check_distribution,
)

_VERSION = "1.2.3"
_RELEASE = f"focusvm-{_VERSION}"


def _prepare(
    directory: Path,
    *,
    source_extra: str | None = None,
    wheel_extra: str | None = None,
    link: bool = False,
) -> None:
    source_files = sorted(_SOURCE_REQUIRED)
    if source_extra is not None:
        source_files.append(source_extra)
    with open_tar(directory / f"{_RELEASE}.tar.gz", "w:gz") as source:
        for file in source_files:
            entry = TarInfo(f"{_RELEASE}/{file}")
            if file == source_extra and link:
                entry.type = SYMTYPE
                entry.linkname = "/test/local-data"
                source.addfile(entry)
            else:
                payload = b"example\n"
                entry.size = len(payload)
                source.addfile(entry, BytesIO(payload))

    with ZipFile(directory / f"{_RELEASE}-py3-none-any.whl", "w") as wheel:
        wheel.writestr("focusvm/cli.py", "example\n")
        for file in sorted(_WHEEL_METADATA):
            wheel.writestr(f"{_RELEASE}.dist-info/{file}", "example\n")
        if wheel_extra is not None:
            wheel.writestr(wheel_extra, "example\n")


def test_accepts_expected_distribution_contents(tmp_path: Path) -> None:
    _prepare(tmp_path)
    assert check_distribution(tmp_path) == _VERSION


@pytest.mark.parametrize(
    "path",
    [
        "analysis.md",
        "docs/analysis.md",
        ".private/config.py",
        "guest-output/example.txt",
        "guest/example.qcow2",
        "guest/publisher.gpg",
        "src/focusvm/__pycache__/cli.py",
        "docs/../private.py",
    ],
)
def test_rejects_private_or_generated_source_contents(
    tmp_path: Path, path: str
) -> None:
    _prepare(tmp_path, source_extra=path)
    with pytest.raises(ValueError, match="Unexpected"):
        check_distribution(tmp_path)


def test_rejects_source_archive_link(tmp_path: Path) -> None:
    _prepare(tmp_path, source_extra="docs/link.md", link=True)
    with pytest.raises(ValueError, match="Unexpected source archive member"):
        check_distribution(tmp_path)


def test_rejects_names_in_wheel_absent_from_source(tmp_path: Path) -> None:
    _prepare(tmp_path, wheel_extra="focusvm/unexpected.py")
    with pytest.raises(ValueError, match="Unexpected wheel files"):
        check_distribution(tmp_path)
