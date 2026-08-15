import gzip
import hashlib
import json
from pathlib import Path

import pytest

import maxmcp.worker.artifacts as artifacts_module
from maxmcp.worker.artifacts import build_artifacts, download_source, upload_signed_artifact
from maxmcp.worker.motion_pipeline import PipelineArtifacts


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


def test_download_rejects_oversized_or_non_https_sources(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        download_source("file:///secret", tmp_path / "source.mp4")
    with pytest.raises(ValueError, match="too large"):
        download_source(
            "https://signed.test/source",
            tmp_path / "source.mp4",
            max_bytes=4,
            opener=lambda *_args, **_kwargs: DownloadResponse(b"video"),
        )


def test_download_transparently_expands_and_validates_tracking_gzip(tmp_path: Path) -> None:
    target = tmp_path / "original.rtmw3d.json"
    body = b'{"schema":"artoke.rtmw3d.v1"}'

    download_source(
        "https://signed.test/tracking",
        target,
        opener=lambda *_args, **_kwargs: DownloadResponse(gzip.compress(body, mtime=0)),
    )

    assert target.read_bytes() == body
    assert json.loads(target.read_text(encoding="utf-8"))["schema"] == "artoke.rtmw3d.v1"


def test_tracking_download_enforces_stored_and_decompressed_boundaries(tmp_path: Path) -> None:
    target = tmp_path / "original.rtmw3d.json"
    readable = b'{"schema":"artoke.rtmw3d.v1"}'
    stored = gzip.compress(readable, mtime=0)

    download_source(
        "https://signed.test/tracking",
        target,
        max_bytes=len(stored),
        max_decompressed_json_bytes=len(readable),
        opener=lambda *_args, **_kwargs: DownloadResponse(stored),
    )
    assert target.read_bytes() == readable

    with pytest.raises(ValueError, match="source download is too large"):
        download_source(
            "https://signed.test/tracking",
            target,
            max_bytes=len(stored) - 1,
            max_decompressed_json_bytes=len(readable),
            opener=lambda *_args, **_kwargs: DownloadResponse(stored),
        )

    with pytest.raises(ValueError, match="decompressed JSON is too large"):
        download_source(
            "https://signed.test/tracking",
            target,
            max_bytes=len(stored),
            max_decompressed_json_bytes=len(readable) - 1,
            opener=lambda *_args, **_kwargs: DownloadResponse(stored),
        )


def test_download_bounds_tracking_expansion_and_removes_invalid_json(tmp_path: Path) -> None:
    target = tmp_path / "original.rtmw3d.json"
    with pytest.raises(ValueError, match="too large"):
        download_source(
            "https://signed.test/tracking",
            target,
            max_decompressed_json_bytes=8,
            opener=lambda *_args, **_kwargs: DownloadResponse(
                gzip.compress(b'{"schema":"artoke.rtmw3d.v1"}', mtime=0)
            ),
        )
    assert not target.exists()

    with pytest.raises(ValueError, match="JSON"):
        download_source(
            "https://signed.test/tracking",
            target,
            opener=lambda *_args, **_kwargs: DownloadResponse(
                gzip.compress(b"not-json", mtime=0)
            ),
        )
    assert not target.exists()


def test_download_keeps_video_bytes_identical_even_with_gzip_magic(tmp_path: Path) -> None:
    target = tmp_path / "source.mp4"
    body = gzip.compress(b"video", mtime=0)

    download_source(
        "https://signed.test/source",
        target,
        opener=lambda *_args, **_kwargs: DownloadResponse(body),
    )

    assert target.read_bytes() == body


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
        edit_revision=3,
        process_runner=ffmpeg,
    )

    assert [(item.kind, item.path.name) for item in artifacts] == [
        ("bvh", "motion.bvh"),
        ("rtmw3d_json", "motion.rtmw3d.json.gz"),
        ("thumbnail", "thumbnail.webp"),
        ("metadata", "metadata.json"),
    ]
    assert all(item.size_bytes > 0 and len(item.sha256) == 64 for item in artifacts)
    compressed = tmp_path / "result" / "motion.rtmw3d.json.gz"
    compressed_bytes = compressed.read_bytes()
    tracking_artifact = next(item for item in artifacts if item.kind == "rtmw3d_json")
    assert compressed_bytes.startswith(b"\x1f\x8b")
    assert compressed_bytes[4:8] == b"\x00\x00\x00\x00"
    assert gzip.decompress(compressed_bytes) == body.read_bytes()
    assert tracking_artifact.size_bytes == len(compressed_bytes)
    assert tracking_artifact.sha256 == hashlib.sha256(compressed_bytes).hexdigest()
    metadata = json.loads((tmp_path / "result" / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["fps"] == 30
    assert metadata["frame_count"] == 12
    assert metadata["duration_seconds"] == 4.0
    assert metadata["editRevision"] == 3
    assert metadata["sha256"]["source"]
    assert metadata["warnings"] == []


def test_build_artifacts_preserves_valid_legacy_identity_tracking(tmp_path: Path) -> None:
    video = tmp_path / "source.mp4"
    video.write_bytes(b"video")
    body = tmp_path / "walk_rtmw3d.json"
    body.write_text('{"schema":"artoke.rtmw3d.v1"}', encoding="utf-8")
    bvh = tmp_path / "walk.bvh"
    bvh.write_text(
        "HIERARCHY\nROOT Pelvis\nMOTION\nFrames: 1\nFrame Time: 0.0333333333\n",
        encoding="utf-8",
    )
    trace = tmp_path / "trace.json"
    trace.write_text("{}", encoding="utf-8")

    def ffmpeg(command, **_kwargs):
        Path(command[-1]).write_bytes(b"webp")
        return type("Result", (), {"returncode": 0, "stderr": ""})()

    built = build_artifacts(
        video,
        PipelineArtifacts(body, bvh, trace, 1),
        tmp_path / "result",
        1.0,
        tracking_encoding="identity",
        process_runner=ffmpeg,
    )

    tracking = next(item for item in built if item.kind == "rtmw3d_json")
    assert tracking.path.name == "motion.rtmw3d.json"
    assert tracking.path.read_bytes() == body.read_bytes()


def test_build_artifacts_rejects_invalid_legacy_identity_json(tmp_path: Path) -> None:
    video = tmp_path / "source.mp4"
    video.write_bytes(b"video")
    body = tmp_path / "invalid.json"
    body.write_text("not-json", encoding="utf-8")
    bvh = tmp_path / "walk.bvh"
    bvh.write_text(
        "HIERARCHY\nROOT Pelvis\nMOTION\nFrames: 1\nFrame Time: 0.0333333333\n",
        encoding="utf-8",
    )
    trace = tmp_path / "trace.json"
    trace.write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="tracking JSON is invalid"):
        build_artifacts(
            video,
            PipelineArtifacts(body, bvh, trace, 1),
            tmp_path / "result",
            1.0,
            tracking_encoding="identity",
            process_runner=lambda *_args, **_kwargs: None,
        )


def test_tracking_gzip_is_deterministic_and_capped_at_45_mib(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert artifacts_module.MAX_TRACKING_COMPRESSED_BYTES == 45 * 1024 * 1024
    video = tmp_path / "source.mp4"
    video.write_bytes(b"video")
    body = tmp_path / "walk_rtmw3d.json"
    body.write_text('{"schema":"artoke.rtmw3d.v1"}', encoding="utf-8")
    bvh = tmp_path / "walk.bvh"
    bvh.write_text(
        "HIERARCHY\nROOT Pelvis\nMOTION\nFrames: 1\nFrame Time: 0.0333333333\n",
        encoding="utf-8",
    )
    trace = tmp_path / "trace.json"
    trace.write_text("{}", encoding="utf-8")

    def ffmpeg(command, **_kwargs):
        Path(command[-1]).write_bytes(b"webp")
        return type("Result", (), {"returncode": 0, "stderr": ""})()

    pipeline = PipelineArtifacts(body, bvh, trace, 1)
    first = build_artifacts(video, pipeline, tmp_path / "first", 1.0, process_runner=ffmpeg)
    second = build_artifacts(video, pipeline, tmp_path / "second", 1.0, process_runner=ffmpeg)
    first_body = next(item for item in first if item.kind == "rtmw3d_json")
    second_body = next(item for item in second if item.kind == "rtmw3d_json")
    assert first_body.path.read_bytes() == second_body.path.read_bytes()
    assert first_body.sha256 == second_body.sha256

    monkeypatch.setattr(artifacts_module, "MAX_TRACKING_COMPRESSED_BYTES", 8)
    with pytest.raises(ValueError, match="45 MiB"):
        build_artifacts(video, pipeline, tmp_path / "limited", 1.0, process_runner=ffmpeg)
    with pytest.raises(ValueError, match="45 MiB"):
        build_artifacts(
            video,
            pipeline,
            tmp_path / "limited-identity",
            1.0,
            tracking_encoding="identity",
            process_runner=ffmpeg,
        )

    monkeypatch.setattr(
        artifacts_module,
        "MAX_TRACKING_COMPRESSED_BYTES",
        45 * 1024 * 1024,
    )
    monkeypatch.setattr(artifacts_module, "MAX_TRACKING_DECOMPRESSED_BYTES", 8)
    with pytest.raises(ValueError, match="256 MiB"):
        build_artifacts(
            video,
            pipeline,
            tmp_path / "limited-readable",
            1.0,
            process_runner=ffmpeg,
        )


def test_signed_upload_streams_file_with_put(tmp_path: Path) -> None:
    artifact = tmp_path / "motion.bvh"
    artifact.write_bytes(b"bvh-data")
    requests = []

    def opener(request, timeout):
        requests.append((request, timeout, b"".join(request.data)))
        return DownloadResponse(b"{}")

    upload_signed_artifact(
        "https://storage.test/upload?token=secret",
        artifact,
        "application/octet-stream",
        opener=opener,
    )

    request, timeout, body = requests[0]
    assert request.method == "PUT"
    assert request.headers["Content-type"] == "application/octet-stream"
    assert request.headers["Content-length"] == str(len(body))
    assert body == b"bvh-data"
    assert timeout == 120


@pytest.mark.parametrize(
    ("filename", "expected_content_type"),
    [
        ("motion.rtmw3d.json", "application/json"),
        ("motion.rtmw3d.json.gz", "application/gzip"),
    ],
)
def test_signed_tracking_upload_uses_encoding_content_type(
    tmp_path: Path,
    filename: str,
    expected_content_type: str,
) -> None:
    artifact = tmp_path / filename
    artifact.write_bytes(b"tracking")
    requests = []

    def opener(request, **_kwargs):
        requests.append(request)
        return DownloadResponse(b"{}")

    upload_signed_artifact(
        "https://storage.test/upload?token=secret",
        artifact,
        "application/json",
        opener=opener,
    )

    assert requests[0].headers["Content-type"] == expected_content_type


def test_signed_upload_rejects_non_https_url(tmp_path: Path) -> None:
    artifact = tmp_path / "motion.bvh"
    artifact.write_bytes(b"bvh")
    with pytest.raises(ValueError, match="HTTPS"):
        upload_signed_artifact("file:///tmp/result", artifact, "application/octet-stream")
