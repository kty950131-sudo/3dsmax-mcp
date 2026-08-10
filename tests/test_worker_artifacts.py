import json
from pathlib import Path

import pytest

from src.worker.artifacts import build_artifacts, download_source
from src.worker.motion_pipeline import PipelineArtifacts


class DownloadResponse:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size: int = -1) -> bytes:
        if not self.body:
            return b""
        if size < 0:
            data, self.body = self.body, b""
        else:
            data, self.body = self.body[:size], self.body[size:]
        return data


def test_download_rejects_a_source_hash_mismatch(tmp_path: Path) -> None:
    target = tmp_path / "source.mp4"
    with pytest.raises(ValueError, match="SHA-256"):
        download_source(
            "https://signed.test/source?token=secret",
            target,
            expected_sha256="0" * 64,
            opener=lambda *_args, **_kwargs: DownloadResponse(b"video"),
        )
    assert not target.exists()


def test_build_artifacts_creates_four_fixed_outputs(tmp_path: Path) -> None:
    video = tmp_path / "source.mp4"
    video.write_bytes(b"video")
    body = tmp_path / "walk_rtmw3d.json"
    body.write_text('{"schema":"artoke.rtmw3d.v1"}', encoding="utf-8")
    bvh = tmp_path / "walk.bvh"
    bvh.write_text(
        "HIERARCHY\nROOT Pelvis\nMOTION\nFrames: 12\nFrame Time: 0.0333333333\n",
        encoding="utf-8",
    )
    trace = tmp_path / "trace.json"
    trace.write_text('{"backend":"OpenMMLab RTMW3D-L"}', encoding="utf-8")

    def ffmpeg(command, **kwargs):
        assert "-ss" in command
        Path(command[-1]).write_bytes(b"webp")
        return type("Result", (), {"returncode": 0, "stderr": ""})()

    artifacts = build_artifacts(
        video,
        PipelineArtifacts(body, bvh, trace, 12),
        tmp_path / "result",
        duration_seconds=4.0,
        process_runner=ffmpeg,
    )

    assert [(item.kind, item.path.name) for item in artifacts] == [
        ("bvh", "motion.bvh"),
        ("rtmw3d_json", "motion.rtmw3d.json"),
        ("thumbnail", "thumbnail.webp"),
        ("metadata", "metadata.json"),
    ]
    assert all(item.size_bytes > 0 and len(item.sha256) == 64 for item in artifacts)
    metadata = json.loads((tmp_path / "result" / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["fps"] == 30
    assert metadata["frame_count"] == 12
    assert metadata["duration_seconds"] == 4.0
    assert metadata["sha256"]["source"]
    assert metadata["warnings"] == []
