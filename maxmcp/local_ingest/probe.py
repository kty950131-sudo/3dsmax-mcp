"""Strict, bounded ffprobe validation for local motion sources."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable


MAX_PROBE_OUTPUT_BYTES = 1024 * 1024
MAX_DURATION_SECONDS = 300.0
MAX_DIMENSION = 16_384
_EXTENSIONS = {".mp4": "video/mp4", ".mov": "video/quicktime", ".avi": "video/x-msvideo"}
_FORMATS = {
    ".mp4": {"mov", "mp4", "m4a", "3gp", "3g2", "mj2"},
    ".mov": {"mov", "mp4", "m4a", "3gp", "3g2", "mj2"},
    ".avi": {"avi"},
}
_CODECS = {
    ".mp4": {"h264", "hevc", "mpeg4", "av1", "vp9"},
    ".mov": {"h264", "hevc", "mpeg4", "mjpeg", "av1"},
    ".avi": {"h264", "mpeg4", "mjpeg"},
}


class ProbeRejected(RuntimeError):
    """A stable ffprobe rejection safe to show or log."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class VideoProbe:
    duration_seconds: float
    format_name: str
    codec_name: str
    width: int
    height: int
    content_type: str
    extension: str


@dataclass(frozen=True)
class _ProcessResult:
    stdout: bytes
    stderr: bytes
    returncode: int


def _read_limited(stream: Any, limit: int, output: list[bytes], overflow: threading.Event) -> None:
    chunks: list[bytes] = []
    total = 0
    while True:
        block = stream.read(min(64 * 1024, limit + 1 - total))
        if not block:
            break
        total += len(block)
        if total > limit:
            overflow.set()
            break
        chunks.append(block)
    output.append(b"".join(chunks))


def _run_bounded(
    command: list[str], *, timeout: float, max_output_bytes: int
) -> _ProcessResult:
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    stdout: list[bytes] = []
    stderr: list[bytes] = []
    overflow = threading.Event()
    readers = (
        threading.Thread(target=_read_limited, args=(process.stdout, max_output_bytes, stdout, overflow), daemon=True),
        threading.Thread(target=_read_limited, args=(process.stderr, max_output_bytes, stderr, overflow), daemon=True),
    )
    for reader in readers:
        reader.start()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        raise
    finally:
        for reader in readers:
            reader.join(timeout=1)
    if overflow.is_set():
        process.kill() if process.poll() is None else None
        raise ProbeRejected("video_probe_invalid")
    return _ProcessResult(stdout[0] if stdout else b"", stderr[0] if stderr else b"", process.returncode)


def probe_video(
    source: Path,
    display_name: str,
    *,
    run: Callable[..., Any] = _run_bounded,
    timeout: float = 15.0,
    max_output_bytes: int = MAX_PROBE_OUTPUT_BYTES,
) -> VideoProbe:
    extension = Path(display_name).suffix.lower()
    if extension not in _EXTENSIONS:
        raise ProbeRejected("video_container_mismatch")
    command = [
        "ffprobe", "-v", "error", "-show_entries",
        "format=format_name,duration:stream=index,codec_type,codec_name,width,height,disposition",
        "-of", "json", str(source),
    ]
    try:
        result = run(command, timeout=timeout, max_output_bytes=max_output_bytes)
    except ProbeRejected:
        raise
    except (OSError, subprocess.SubprocessError):
        raise ProbeRejected("video_probe_failed") from None
    stdout = getattr(result, "stdout", b"")
    stderr = getattr(result, "stderr", b"")
    if not isinstance(stdout, bytes) or not isinstance(stderr, bytes):
        raise ProbeRejected("video_probe_invalid")
    if len(stdout) > max_output_bytes or len(stderr) > max_output_bytes:
        raise ProbeRejected("video_probe_invalid")
    if getattr(result, "returncode", None) != 0:
        raise ProbeRejected("video_probe_failed")
    try:
        payload = json.loads(stdout)
        if not isinstance(payload, dict) or set(payload) != {"format", "streams"}:
            raise TypeError
        format_data = payload["format"]
        streams = payload["streams"]
        if not isinstance(format_data, dict) or not isinstance(streams, list):
            raise TypeError
        if set(format_data) != {"duration", "format_name"}:
            raise TypeError
        duration = float(format_data["duration"])
        format_name = format_data["format_name"]
        if not isinstance(format_name, str) or not format_name:
            raise TypeError
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError, OverflowError):
        raise ProbeRejected("video_probe_invalid") from None
    if not math.isfinite(duration) or not 0 < duration <= MAX_DURATION_SECONDS:
        raise ProbeRejected("video_duration_invalid")
    detected_formats = set(format_name.lower().split(","))
    if not detected_formats.intersection(_FORMATS[extension]):
        raise ProbeRejected("video_container_mismatch")

    candidates: list[dict[str, object]] = []
    indexes: set[int] = set()
    for stream in streams:
        if not isinstance(stream, dict):
            raise ProbeRejected("video_stream_invalid")
        index = stream.get("index")
        codec_type = stream.get("codec_type")
        codec_name = stream.get("codec_name")
        disposition = stream.get("disposition")
        attached_pic = disposition.get("attached_pic") if isinstance(disposition, dict) else None
        if (
            not isinstance(index, int)
            or isinstance(index, bool)
            or index < 0
            or index in indexes
            or not isinstance(codec_type, str)
            or not codec_type
            or not isinstance(codec_name, str)
            or not codec_name
            or not isinstance(disposition, dict)
            or not isinstance(attached_pic, int)
            or isinstance(attached_pic, bool)
            or attached_pic not in {0, 1}
        ):
            raise ProbeRejected("video_stream_invalid")
        indexes.add(index)
        if codec_type != "video":
            continue
        if attached_pic != 0:
            continue
        candidates.append(stream)
    if len(candidates) != 1:
        raise ProbeRejected("video_stream_invalid")
    stream = candidates[0]
    codec = stream.get("codec_name")
    width = stream.get("width")
    height = stream.get("height")
    if (
        not isinstance(codec, str)
        or codec.lower() not in _CODECS[extension]
        or not isinstance(width, int)
        or isinstance(width, bool)
        or not isinstance(height, int)
        or isinstance(height, bool)
        or not 0 < width <= MAX_DIMENSION
        or not 0 < height <= MAX_DIMENSION
    ):
        raise ProbeRejected("video_stream_invalid")
    return VideoProbe(
        duration, format_name, codec.lower(), width, height, _EXTENSIONS[extension], extension
    )
