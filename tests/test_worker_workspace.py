from pathlib import Path
import os
import time

import pytest

from src.worker.workspace import JobWorkspace, cleanup_stale


JOB_ID = "00000000-0000-4000-8000-000000000001"


def test_workspace_stays_below_root_and_cleans_on_exit(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    with JobWorkspace.open(root, JOB_ID) as workspace:
        assert workspace.path.parent == root.resolve()
        (workspace.path / "source.mp4").write_bytes(b"video")
        path = workspace.path

    assert not path.exists()


@pytest.mark.parametrize("job_id", ["../escape", "not-a-uuid", "", "a/b"])
def test_workspace_rejects_unsafe_job_ids(tmp_path: Path, job_id: str) -> None:
    with pytest.raises(ValueError, match="job id"):
        JobWorkspace.open(tmp_path, job_id)


def test_stale_cleanup_only_removes_old_job_directories(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    stale = root / JOB_ID
    fresh = root / "00000000-0000-4000-8000-000000000002"
    foreign = root / "notes"
    stale.mkdir(parents=True)
    fresh.mkdir()
    foreign.mkdir()
    old = time.time() - 90_000
    os.utime(stale, (old, old))

    removed = cleanup_stale(root, older_than_seconds=86_400)

    assert removed == [stale.resolve()]
    assert not stale.exists()
    assert fresh.exists()
    assert foreign.exists()
