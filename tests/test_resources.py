import hashlib
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from focusvm import resources
from focusvm.resources import ResourceError, ResourceLibrary, ResourceUnavailable


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("Reference.PDF", "pdf"),
        ("guide.htm", "html"),
        ("guide.html", "html"),
        ("notes.markdown", "markdown"),
        ("wiki.zim", "zim"),
        ("updates.bundle", "git-bundle"),
        ("example-1.0-py3-none-any.whl", "wheel"),
    ],
)
def test_import_copies_bytes_and_persists_metadata(
    tmp_path: Path, name: str, kind: str
) -> None:
    data_dir = tmp_path / "data"
    library = ResourceLibrary(data_dir)
    source = tmp_path / name
    content = b"offline material"
    source.write_bytes(content)

    added = library.add(source)
    source.write_bytes(b"changed")

    assert added.kind == kind
    assert added.name == name
    assert added.size_bytes == len(content)
    assert added.sha256 == hashlib.sha256(content).hexdigest()
    assert ResourceLibrary(data_dir).list() == (added,)
    entry = data_dir / "resources" / added.id
    assert stat.S_IMODE(entry.stat().st_mode) == 0o700
    assert stat.S_IMODE((entry / name).stat().st_mode) == 0o400
    assert (entry / name).read_bytes() == content


def test_search_and_removal_preserve_other_entries(tmp_path: Path) -> None:
    library = ResourceLibrary(tmp_path / "data")
    alpha = tmp_path / "Alpha notes.md"
    beta = tmp_path / "beta.pdf"
    alpha.write_text("alpha")
    beta.write_bytes(b"beta")
    beta_resource = library.add(beta)
    alpha_resource = library.add(alpha)

    assert library.list() == (alpha_resource, beta_resource)
    assert library.search("  NOTE ") == (alpha_resource,)
    assert library.search("missing") == ()
    assert library.remove(alpha_resource.id) == alpha_resource
    assert library.list() == (beta_resource,)
    assert alpha.read_text() == "alpha"
    with pytest.raises(ResourceError, match="Unknown resource"):
        library.remove(alpha_resource.id)


def test_rejects_unsupported_files_links_and_fifos(tmp_path: Path) -> None:
    library = ResourceLibrary(tmp_path / "data")
    unsupported = tmp_path / "photo.jpg"
    unsupported.touch()
    with pytest.raises(ResourceError, match="Use a PDF"):
        library.add(unsupported)

    link = tmp_path / "link.pdf"
    link.symlink_to(unsupported)
    with pytest.raises(ResourceError, match="Could not read"):
        library.add(link)

    fifo = tmp_path / "fifo.pdf"
    os.mkfifo(fifo)
    with pytest.raises(ResourceError, match="regular resource file"):
        library.add(fifo)

    assert library.list() == ()


def test_rejects_symlinked_resource_directory(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    (data_dir / "resources").symlink_to(other)
    with pytest.raises(ResourceUnavailable, match="safe to write"):
        ResourceLibrary(data_dir).list()
    assert not tuple(other.iterdir())


def test_host_viewers_get_literal_absolute_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def lookup(name: str) -> str:
        return f"/bin/{name}"

    monkeypatch.setattr(shutil, "which", lookup)
    library = ResourceLibrary(tmp_path / "data")
    for name, executable in (
        ("- a (β).md", "xdg-open"),
        ("archive.zim", "kiwix-desktop"),
    ):
        source = tmp_path / name
        source.touch()
        added = library.add(source)
        program, argv = library.open_command(added.id)
        assert program == f"/bin/{executable}"
        assert argv == [str(tmp_path / "data" / "resources" / added.id / name)]

    source = tmp_path / "updates.bundle"
    source.touch()
    with pytest.raises(ResourceError, match="inside the guest"):
        library.open_command(library.add(source).id)
    with pytest.raises(ResourceError, match="Invalid resource ID"):
        library.open_command("../escape")


def test_missing_viewer_is_actionable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    library = ResourceLibrary(tmp_path / "data")
    source = tmp_path / "archive.zim"
    source.touch()
    with pytest.raises(ResourceUnavailable, match="kiwix-desktop"):
        library.open_command(library.add(source).id)


def test_export_refuses_tampered_bytes_and_preserves_existing_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def lookup(name: str) -> str:
        return f"/bin/{name}"

    def unexpected_build(executable: str, contents: Path, image: Path) -> None:
        pytest.fail("Generated an ISO after validation failed")

    monkeypatch.setattr(shutil, "which", lookup)
    monkeypatch.setattr(resources, "_build_iso", unexpected_build)
    data_dir = tmp_path / "data"
    library = ResourceLibrary(data_dir)
    source = tmp_path / "notes.md"
    source.write_bytes(b"good")
    resource = library.add(source)
    stored = data_dir / "resources" / resource.id / source.name
    stored.chmod(0o600)
    stored.write_bytes(b"bad!")
    output = tmp_path / "docs.iso"
    output.write_bytes(b"keep me")
    with pytest.raises(ResourceError, match="already exists"):
        library.build_iso([resource.id], output)
    assert output.read_bytes() == b"keep me"
    output.unlink()
    with pytest.raises(ResourceError, match="content changed"):
        library.build_iso([resource.id], output)
    assert not output.exists()
    assert not tuple(tmp_path.glob(".focusvm-export-*"))


def test_export_does_not_publish_partial_iso(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def lookup(name: str) -> str:
        return f"/bin/{name}"

    def failing_build(executable: str, contents: Path, image: Path) -> None:
        image.write_bytes(b"incomplete")
        raise ResourceUnavailable("simulated tool failure")

    monkeypatch.setattr(shutil, "which", lookup)
    monkeypatch.setattr(resources, "_build_iso", failing_build)
    library = ResourceLibrary(tmp_path / "data")
    source = tmp_path / "notes.md"
    source.touch()
    added = library.add(source)
    output = tmp_path / "docs.iso"
    with pytest.raises(ResourceUnavailable, match="simulated"):
        library.build_iso([added.id], output)
    assert not output.exists()
    assert not tuple(tmp_path.glob(".focusvm-export-*"))


@pytest.mark.skipif(not shutil.which("xorriso"), reason="xorriso is unavailable")
def test_native_iso_is_repeatable_and_preserves_filenames(tmp_path: Path) -> None:
    library = ResourceLibrary(tmp_path / "data")
    doc_name = "- a (β).md"
    source = tmp_path / doc_name
    source.write_bytes(b"first copy")
    first = library.add(source)
    source.write_bytes(b"second copy")
    second = library.add(source)
    wheel_name = "example-1.0-py3-none-any.whl"
    wheel = tmp_path / wheel_name
    wheel.write_bytes(b"an opaque wheel")
    third = library.add(wheel)
    image = library.build_iso([first.id, second.id, third.id], tmp_path / "docs.iso")
    repeat = library.build_iso(
        [third.id, first.id, second.id, first.id], tmp_path / "repeat.iso"
    )
    assert image.read_bytes() == repeat.read_bytes()
    assert stat.S_IMODE(image.stat().st_mode) == 0o600

    extracted = tmp_path / "extracted"
    xorriso = shutil.which("xorriso")
    assert xorriso is not None
    result = subprocess.run(
        [
            xorriso,
            "-indev",
            str(image),
            "-osirrox",
            "on",
            "-extract",
            "/",
            str(extracted),
        ],
        capture_output=True,
        check=True,
        timeout=30,
    )
    assert b"FOCUSVM_DOCS" in result.stderr
    assert (extracted / first.id / doc_name).read_bytes() == b"first copy"
    assert (extracted / second.id / doc_name).read_bytes() == b"second copy"
    assert (extracted / third.id / wheel_name).read_bytes() == b"an opaque wheel"
    index = (extracted / "index.txt").read_text()
    assert f"{third.id}/{wheel_name}" in index
