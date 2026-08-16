import json
from pathlib import Path
import subprocess

import pytest

from maxmcp.local_ingest.probe import ProbeRejected, probe_video


def _payload(
    *,
    duration: object = "12.5",
    format_name: object = "mov,mp4,m4a,3gp,3g2,mj2",
    streams: object | None = None,
) -> bytes:
    if streams is None:
        streams = [
            {
                "index": 0,
                "codec_type": "video",
                "codec_name": "h264",
                "width": 1920,
                "height": 1080,
                "disposition": {"attached_pic": 0},
            }
        ]
    return json.dumps(
        {"format": {"duration": duration, "format_name": format_name}, "streams": streams}
    ).encode()


class _Result:
    def __init__(self, stdout: bytes, stderr: bytes = b"", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def test_probe_uses_shell_free_bounded_ffprobe_and_returns_detected_video(tmp_path: Path) -> None:
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"video")
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return _Result(_payload())

    info = probe_video(source, "clip.mp4", run=run)

    command, kwargs = calls[0]
    assert command[0] == "ffprobe"
    assert command[-1] == str(source)
    assert kwargs == {"timeout": 15.0, "max_output_bytes": 1024 * 1024}
    assert info.duration_seconds == 12.5
    assert info.width == 1920
    assert info.height == 1080
    assert info.content_type == "video/mp4"


def test_probe_accepts_ffmpeg8_output_and_requests_stream_disposition(tmp_path: Path) -> None:
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"video")
    calls = []

    def run(command, **_kwargs):
        calls.append(command)
        return _Result(json.dumps({
            "programs": [],
            "stream_groups": [],
            "streams": [
                {
                    "index": 0,
                    "codec_type": "video",
                    "codec_name": "vp9",
                    "width": 608,
                    "height": 1080,
                    "disposition": {"attached_pic": 0},
                },
                {
                    "index": 1,
                    "codec_type": "audio",
                    "codec_name": "opus",
                    "disposition": {"attached_pic": 0},
                },
            ],
            "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "39.414000"},
        }).encode())

    info = probe_video(source, "clip.mp4", run=run)

    assert info.codec_name == "vp9"
    assert info.duration_seconds == 39.414
    entries = calls[0][calls[0].index("-show_entries") + 1]
    assert "stream_disposition=attached_pic" in entries
    assert ",disposition" not in entries


@pytest.mark.parametrize("duration", ["0", "-1", "nan", "inf", "301"])
def test_probe_rejects_nonpositive_nonfinite_and_over_limit_duration(
    tmp_path: Path, duration: str
) -> None:
    source = tmp_path / "clip.mp4"; source.write_bytes(b"x")
    with pytest.raises(ProbeRejected, match="video_duration_invalid"):
        probe_video(source, "clip.mp4", run=lambda *_a, **_k: _Result(_payload(duration=duration)))


def test_probe_accepts_exact_300_second_boundary(tmp_path: Path) -> None:
    source = tmp_path / "clip.mp4"; source.write_bytes(b"x")
    assert probe_video(
        source, "clip.mp4", run=lambda *_a, **_k: _Result(_payload(duration="300"))
    ).duration_seconds == 300.0


@pytest.mark.parametrize(
    "streams",
    [
        [],
        [{"index": 0, "codec_type": "audio", "codec_name": "aac"}],
        [
            {"index": 0, "codec_type": "video", "codec_name": "h264", "width": 10, "height": 10, "disposition": {"attached_pic": 0}},
            {"index": 1, "codec_type": "video", "codec_name": "h264", "width": 10, "height": 10, "disposition": {"attached_pic": 0}},
        ],
        [{"index": 0, "codec_type": "video", "codec_name": "h264", "width": 0, "height": 1080, "disposition": {"attached_pic": 0}}],
        [{"index": 0, "codec_type": "video", "codec_name": "png", "width": 640, "height": 480, "disposition": {"attached_pic": 1}}],
    ],
)
def test_probe_rejects_missing_ambiguous_or_image_only_video(tmp_path: Path, streams: object) -> None:
    source = tmp_path / "clip.mp4"; source.write_bytes(b"x")
    with pytest.raises(ProbeRejected, match="video_stream_invalid"):
        probe_video(source, "clip.mp4", run=lambda *_a, **_k: _Result(_payload(streams=streams)))


@pytest.mark.parametrize(
    "stdout",
    [b"not-json", b"[]", b"{}", b"{" + b"x" * (1024 * 1024 + 1)],
    ids=["invalid-json", "array", "missing-shape", "too-large"],
)
def test_probe_rejects_malformed_or_huge_output(tmp_path: Path, stdout: bytes) -> None:
    source = tmp_path / "clip.mp4"; source.write_bytes(b"x")
    with pytest.raises(ProbeRejected, match="video_probe_invalid"):
        probe_video(source, "clip.mp4", run=lambda *_a, **_k: _Result(stdout))


def test_probe_rejects_timeout_nonzero_and_extension_container_mismatch(tmp_path: Path) -> None:
    source = tmp_path / "clip.mp4"; source.write_bytes(b"x")
    with pytest.raises(ProbeRejected, match="video_probe_failed"):
        probe_video(source, "clip.mp4", run=lambda *_a, **_k: (_ for _ in ()).throw(subprocess.TimeoutExpired("secret", 15)))
    with pytest.raises(ProbeRejected, match="video_probe_failed"):
        probe_video(source, "clip.mp4", run=lambda *_a, **_k: _Result(b"", b"C:\\secret", 1))
    with pytest.raises(ProbeRejected, match="video_container_mismatch"):
        probe_video(source, "clip.mp4", run=lambda *_a, **_k: _Result(_payload(format_name="avi")))


def test_probe_requires_codec_compatible_with_detected_container(tmp_path: Path) -> None:
    source = tmp_path / "clip.avi"; source.write_bytes(b"x")
    streams = [{"index": 0, "codec_type": "video", "codec_name": "hevc", "width": 640, "height": 480, "disposition": {"attached_pic": 0}}]
    with pytest.raises(ProbeRejected, match="video_stream_invalid"):
        probe_video(source, "clip.avi", run=lambda *_a, **_k: _Result(_payload(format_name="avi", streams=streams)))


@pytest.mark.parametrize(
    "bad_stream",
    [
        {"index": True, "codec_type": "audio", "codec_name": "aac", "disposition": {"attached_pic": 0}},
        {"index": 1, "codec_type": "video", "width": 640, "height": 480, "disposition": {"attached_pic": 0}},
        {"index": 1, "codec_type": "audio", "codec_name": "aac", "disposition": {"attached_pic": "0"}},
    ],
    ids=["bool-index", "missing-codec", "invalid-disposition"],
)
def test_probe_rejects_malformed_secondary_stream_before_selecting_primary(
    tmp_path: Path, bad_stream: dict[str, object]
) -> None:
    source = tmp_path / "clip.mp4"; source.write_bytes(b"x")
    primary = {"index": 0, "codec_type": "video", "codec_name": "h264", "width": 640, "height": 480, "disposition": {"attached_pic": 0}}
    with pytest.raises(ProbeRejected, match="video_stream_invalid"):
        probe_video(source, "clip.mp4", run=lambda *_a, **_k: _Result(_payload(streams=[primary, bad_stream])))


def test_probe_rejects_duplicate_stream_indexes(tmp_path: Path) -> None:
    source = tmp_path / "clip.mp4"; source.write_bytes(b"x")
    streams = [
        {"index": 0, "codec_type": "video", "codec_name": "h264", "width": 640, "height": 480, "disposition": {"attached_pic": 0}},
        {"index": 0, "codec_type": "audio", "codec_name": "aac", "disposition": {"attached_pic": 0}},
    ]
    with pytest.raises(ProbeRejected, match="video_stream_invalid"):
        probe_video(source, "clip.mp4", run=lambda *_a, **_k: _Result(_payload(streams=streams)))
