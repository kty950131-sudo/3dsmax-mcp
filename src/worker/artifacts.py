"""Download validation and canonical ARTOKE motion artifact creation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any, Callable
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from src.worker.motion_pipeline import PipelineArtifacts


@dataclass(frozen=True)
class LocalArtifact:
    kind: str
    path: Path
    size_bytes: int
    sha256: str
    format_version: str = "1"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def download_source(
    url: str,
    destination: Path,
    expected_sha256: str | None = None,
    opener: Callable[..., Any] = urlopen,
    max_bytes: int = 500 * 1024 * 1024,
) -> Path:
    _require_secure_url(url)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    digest = hashlib.sha256()
    received = 0
    try:
        with opener(Request(url), timeout=60) as response, temporary.open("wb") as output:
            while block := response.read(1024 * 1024):
                received += len(block)
                if received > max_bytes:
                    raise ValueError("source download is too large")
                output.write(block)
                digest.update(block)
        actual = digest.hexdigest()
        if expected_sha256 is not None and actual != expected_sha256:
            raise ValueError("source SHA-256 mismatch")
        temporary.replace(destination)
        return destination
    except Exception:
        temporary.unlink(missing_ok=True)
        destination.unlink(missing_ok=True)
        raise


def upload_signed_artifact(
    signed_url: str,
    path: Path,
    content_type: str,
    opener: Callable[..., Any] = urlopen,
) -> None:
    _require_secure_url(signed_url)
    def blocks():
        with path.open("rb") as stream:
            while block := stream.read(1024 * 1024):
                yield block

    request = Request(
        signed_url,
        data=blocks(),
        method="PUT",
        headers={
            "Content-Type": content_type,
            "Content-Length": str(path.stat().st_size),
            "x-upsert": "true",
        },
    )
    with opener(request, timeout=120) as response:
        response.read()


def _require_secure_url(url: str) -> None:
    parsed = urlsplit(url)
    local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme != "https" and not (parsed.scheme == "http" and local):
        raise ValueError("signed transfer requires HTTPS")
    if parsed.username or parsed.password:
        raise ValueError("signed transfer URL must not contain credentials")


def _bvh_info(path: Path) -> tuple[int, float]:
    text = path.read_text(encoding="utf-8", errors="strict")
    if "HIERARCHY" not in text or "MOTION" not in text:
        raise ValueError("invalid BVH structure")
    frames = re.search(r"(?m)^Frames:\s*(\d+)\s*$", text)
    frame_time = re.search(r"(?m)^Frame Time:\s*([0-9.]+)\s*$", text)
    if not frames or not frame_time:
        raise ValueError("invalid BVH motion header")
    return int(frames.group(1)), float(frame_time.group(1))


def build_artifacts(
    video: Path,
    pipeline: PipelineArtifacts,
    output_dir: Path,
    duration_seconds: float,
    process_runner: Callable[..., Any] = subprocess.run,
) -> tuple[LocalArtifact, ...]:
    output_dir.mkdir(parents=True, exist_ok=True)
    bvh = output_dir / "motion.bvh"
    body = output_dir / "motion.rtmw3d.json"
    thumbnail = output_dir / "thumbnail.webp"
    metadata_path = output_dir / "metadata.json"
    shutil.copy2(pipeline.bvh, bvh)
    shutil.copy2(pipeline.rtmw3d_json, body)

    frame_count, frame_time = _bvh_info(bvh)
    if frame_count != pipeline.frame_count:
        raise ValueError("BVH frame count mismatch")
    warnings: list[str] = []
    if abs(frame_time - (1 / 30)) > 0.0001:
        warnings.append("bvh_frame_rate_not_30fps")

    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-ss", f"{max(duration_seconds / 2, 0):.3f}",
        "-i", str(video), "-frames:v", "1",
        "-vf", "scale=640:-2", "-y", str(thumbnail),
    ]
    result = process_runner(
        command,
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode != 0 or not thumbnail.is_file():
        raise RuntimeError("ffmpeg thumbnail generation failed")

    trace = json.loads(pipeline.trace.read_text(encoding="utf-8"))
    metadata = {
        "schema": "artoke.motion.metadata.v1",
        "runtime": str(trace.get("backend", "OpenMMLab RTMW3D-L")),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "fps": 30,
        "frame_count": frame_count,
        "duration_seconds": duration_seconds,
        "sha256": {
            "source": sha256_file(video),
            "bvh": sha256_file(bvh),
            "rtmw3d_json": sha256_file(body),
            "thumbnail": sha256_file(thumbnail),
        },
        "warnings": warnings,
    }
    metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    files = (
        ("bvh", bvh),
        ("rtmw3d_json", body),
        ("thumbnail", thumbnail),
        ("metadata", metadata_path),
    )
    return tuple(
        LocalArtifact(kind, path, path.stat().st_size, sha256_file(path))
        for kind, path in files
    )
