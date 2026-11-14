from pathlib import Path
from time import time
from unittest.mock import patch

import pytest

from focusvm.tracking import ActivityQueue, TrackingError, TrackingInputError


def test_activity_is_opt_in_and_records_only_project_relative_files(
    tmp_path: Path,
) -> None:
    queue = ActivityQueue(tmp_path)
    with pytest.raises(TrackingInputError, match="Enable activity"):
        queue.record("study", "src/main.cpp")
    assert not queue.path.exists()

    queue.enable(["study", "study"])
    timestamp = time()
    heartbeat = queue.record("study", "src/main.cpp", timestamp)
    queue.record("study", "src/main.cpp", timestamp)
    assert queue.status().projects == ("study",)
    assert queue.status().queued == (heartbeat,)
    assert queue.path.stat().st_mode & 0o777 == 0o600
    for path in ("/home/focusvm/secret", "../secret", "https://example.test"):
        with pytest.raises(TrackingInputError, match="project-relative"):
            queue.record("study", path)


def test_partial_upload_retains_unconfirmed_activity(tmp_path: Path) -> None:
    queue = ActivityQueue(tmp_path)
    queue.enable(["one", "two"])
    first = queue.record("one", "first.py")
    second = queue.record("one", "second.py")
    excluded = queue.record("two", "third.py")
    queue.enable(["one"])
    key = "temporary-test-credential"

    with patch(
        "focusvm.tracking._send", side_effect=[None, TrackingError("failed")]
    ) as send:
        with pytest.raises(TrackingError, match="failed"):
            queue.sync(key)
        assert [call.args[0] for call in send.call_args_list] == [first, second]

    assert ActivityQueue(tmp_path).status().queued == (second, excluded)
    assert key not in queue.path.read_text()
    with patch("focusvm.tracking._send") as send:
        queue.disable()
        assert queue.sync(key) == 0
        send.assert_not_called()
    queue.clear()
    assert queue.status().queued == ()


def test_secret_service_key_is_not_a_command_argument() -> None:
    from focusvm.tracking import save_api_key

    key = "temporary-test-credential"
    with (
        patch("focusvm.tracking.shutil.which", return_value="/usr/bin/secret-tool"),
        patch("focusvm.tracking.subprocess.run") as run,
    ):
        run.return_value.returncode = 0
        save_api_key(key)
        assert key not in run.call_args.args[0]
        assert run.call_args.kwargs["input"] == key
