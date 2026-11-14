import shutil
import subprocess
from base64 import urlsafe_b64encode
from hashlib import sha256
from pathlib import Path
from zipfile import ZipFile

import pytest

from focusvm.packages import PackageInputError, build_wheel_media


def make_wheel(
    directory: Path,
    name: str = "sample_math",
    version: str = "1.0",
    *,
    metadata_name: str = "sample-math",
    extra_path: str | None = None,
) -> Path:
    filename = f"{name}-{version}-py3-none-any.whl"
    wheel = directory / filename
    info = f"{name}-{version}.dist-info"
    files = {
        f"{name}.py": b"answer = 42\n",
        f"{info}/METADATA": (
            f"Metadata-Version: 2.1\nName: {metadata_name}\nVersion: {version}\n"
        ).encode(),
        f"{info}/WHEEL": b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    if extra_path is not None:
        files[extra_path] = b"entry\n"
    record = f"{info}/RECORD"
    rows: list[str] = []
    for path, contents in files.items():
        digest = urlsafe_b64encode(sha256(contents).digest()).rstrip(b"=").decode()
        rows.append(f"{path},sha256={digest},{len(contents)}\n")
    files[record] = ("".join(rows) + f"{record},,\n").encode()
    with ZipFile(wheel, "w") as archive:
        for path, contents in files.items():
            archive.writestr(path, contents)
    return wheel


def test_build_media_contains_only_selected_wheel(tmp_path: Path) -> None:
    tool = shutil.which("xorriso")
    if tool is None:
        pytest.skip("xorriso is required to verify the ISO contents")
    wheel = make_wheel(tmp_path)
    make_wheel(tmp_path, "other", metadata_name="other")
    output = tmp_path / "wheels.iso"
    assert build_wheel_media([wheel], output) == output
    assert output.stat().st_mode & 0o777 == 0o600

    extracted = tmp_path / "disc"
    subprocess.run(
        [
            tool,
            "-osirrox",
            "on",
            "-indev",
            str(output),
            "-extract",
            "/",
            str(extracted),
        ],
        capture_output=True,
        check=True,
        timeout=10,
    )
    [resource] = extracted.glob("*/*.whl")
    assert resource.name == wheel.name
    assert resource.read_bytes() == wheel.read_bytes()
    assert (
        f"{resource.parent.name}/{resource.name}"
        in (extracted / "index.txt").read_text()
    )


def test_build_media_rejects_ambiguous_distributions(tmp_path: Path) -> None:
    first = make_wheel(tmp_path)
    second = make_wheel(tmp_path, "sample.math", "2.0")
    output = tmp_path / "wheels.iso"
    with pytest.raises(
        PackageInputError, match="Select only one wheel for sample-math"
    ):
        build_wheel_media([first, second], output)
    assert not output.exists()


@pytest.mark.parametrize(
    "invalid", ["not-a-wheel", "wrong-name", "unsafe-member", "symlink"]
)
def test_build_media_rejects_invalid_inputs(tmp_path: Path, invalid: str) -> None:
    if invalid == "not-a-wheel":
        wheel = tmp_path / "sample_math-1.0-py3-none-any.whl"
        wheel.write_text("not a ZIP archive")
    elif invalid == "wrong-name":
        wheel = make_wheel(tmp_path, metadata_name="other")
    elif invalid == "unsafe-member":
        wheel = make_wheel(tmp_path, extra_path="sample/../../outside.py")
    else:
        original = make_wheel(tmp_path)
        original.rename(tmp_path / "original")
        original.symlink_to(tmp_path / "original")
        wheel = original
    output = tmp_path / "wheels.iso"
    with pytest.raises(PackageInputError, match="[Ww]heel|regular file"):
        build_wheel_media([wheel], output)
    assert not output.exists()


def test_build_media_preserves_existing_output(tmp_path: Path) -> None:
    wheel = make_wheel(tmp_path)
    output = tmp_path / "wheels.iso"
    output.write_bytes(b"existing")
    with pytest.raises(PackageInputError, match="Output already exists"):
        build_wheel_media([wheel], output)
    assert output.read_bytes() == b"existing"
