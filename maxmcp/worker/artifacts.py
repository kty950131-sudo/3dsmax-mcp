"""Download validation and canonical ARTOKE motion artifact creation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any, Callable
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from maxmcp.worker.motion_pipeline import PipelineArtifacts


MAX_TRACKING_COMPRESSED_BYTES = 45 * 1024 * 1024
MAX_TRACKING_DECOMPRESSED_BYTES = 256 * 1024 * 1024


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
    max_decompressed_json_bytes: int = MAX_TRACKING_DECOMPRESSED_BYTES,
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
        if destination.suffix.lower() == ".json":
            _write_validated_json(
                temporary,
                destination,
                max_decompressed_json_bytes,
            )
        else:
            temporary.replace(destination)
        return destination
    except Exception:
        temporary.unlink(missing_ok=True)
        destination.with_suffix(destination.suffix + ".decoded.part").unlink(missing_ok=True)
        destination.unlink(missing_ok=True)
        raise


def _write_validated_json(stored: Path, destination: Path, max_bytes: int) -> None:
    if max_bytes < 1:
        raise ValueError("decompressed JSON is too large")
    decoded = destination.with_suffix(destination.suffix + ".decoded.part")
    source = stored
    with stored.open("rb") as stream:
        gzip_encoded = stream.read(2) == b"\x1f\x8b"
    if gzip_encoded:
        received = 0
        try:
            with gzip.open(stored, "rb") as compressed, decoded.open("wb") as output:
                while block := compressed.read(1024 * 1024):
                    received += len(block)
                    if received > max_bytes:
                        raise ValueError("decompressed JSON is too large")
                    output.write(block)
            source = decoded
        except (gzip.BadGzipFile, EOFError, OSError) as exc:
            raise ValueError("tracking gzip is invalid") from exc
    elif stored.stat().st_size > max_bytes:
        raise ValueError("decompressed JSON is too large")

    try:
        with source.open("r", encoding="utf-8") as stream:
            json.load(stream)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("tracking JSON is invalid") from exc
    source.replace(destination)
    if gzip_encoded:
        stored.unlink(missing_ok=True)


def upload_signed_artifact(
    signed_url: str,
    path: Path,
    content_type: str,
    opener: Callable[..., Any] = urlopen,
) -> None:
    _require_secure_url(signed_url)
    if path.name.endswith(".json.gz"):
        content_type = "application/gzip"
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


MAX_RETAINED_THUMBNAIL_BYTES = 5 * 1024 * 1024
MAX_RETAINED_METADATA_BYTES = 1024 * 1024
_SOURCE_SHA256 = re.compile(r"[0-9a-f]{64}")


def _prepare_motion_outputs(
    pipeline: PipelineArtifacts,
    output_dir: Path,
    tracking_encoding: str,
) -> tuple[Path, Path, int, list[str]]:
    if tracking_encoding not in {"identity", "gzip_v1"}:
        raise ValueError("unsupported tracking encoding")
    if pipeline.rtmw3d_json.stat().st_size > MAX_TRACKING_DECOMPRESSED_BYTES:
        raise ValueError("decompressed RTMW3D JSON exceeds 256 MiB")
    try:
        with pipeline.rtmw3d_json.open("r", encoding="utf-8") as source:
            json.load(source)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("tracking JSON is invalid") from exc

    output_dir.mkdir(parents=True, exist_ok=True)
    bvh = output_dir / "motion.bvh"
    body = output_dir / (
        "motion.rtmw3d.json.gz"
        if tracking_encoding == "gzip_v1"
        else "motion.rtmw3d.json"
    )
    shutil.copy2(pipeline.bvh, bvh)
    if tracking_encoding == "gzip_v1":
        with pipeline.rtmw3d_json.open("rb") as source, body.open("wb") as output:
            with gzip.GzipFile(filename="", mode="wb", fileobj=output, mtime=0) as compressed:
                shutil.copyfileobj(source, compressed, length=1024 * 1024)
    else:
        shutil.copyfile(pipeline.rtmw3d_json, body)
    if body.stat().st_size > MAX_TRACKING_COMPRESSED_BYTES:
        body.unlink(missing_ok=True)
        raise ValueError("stored RTMW3D JSON exceeds 45 MiB")

    frame_count, frame_time = _bvh_info(bvh)
    if frame_count != pipeline.frame_count:
        raise ValueError("BVH frame count mismatch")
    warnings: list[str] = []
    if abs(frame_time - (1 / 30)) > 0.0001:
        warnings.append("bvh_frame_rate_not_30fps")
    return bvh, body, frame_count, warnings


def _write_metadata(
    metadata_path: Path,
    trace_path: Path,
    frame_count: int,
    duration_seconds: float,
    edit_revision: int,
    source_sha256: str,
    bvh: Path,
    body: Path,
    thumbnail: Path,
    warnings: list[str],
) -> None:
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    metadata = {
        "schema": "artoke.motion.metadata.v1",
        "runtime": str(trace.get("backend", "OpenMMLab RTMW3D-L")),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "fps": 30,
        "frame_count": frame_count,
        "duration_seconds": duration_seconds,
        "editRevision": edit_revision,
        "sha256": {
            "source": source_sha256,
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


def _collect_artifacts(
    bvh: Path,
    body: Path,
    thumbnail: Path,
    metadata_path: Path,
) -> tuple[LocalArtifact, ...]:
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


def _retained_source_sha256(metadata_path: Path) -> str:
    try:
        body = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("retained metadata is invalid") from exc
    hashes = body.get("sha256") if isinstance(body, dict) else None
    source = hashes.get("source") if isinstance(hashes, dict) else None
    if not isinstance(source, str) or _SOURCE_SHA256.fullmatch(source) is None:
        raise ValueError("retained metadata is invalid")
    return source


def _copy_retained_thumbnail(retained: Path, destination: Path) -> None:
    try:
        data = retained.read_bytes()
    except OSError as exc:
        raise ValueError("retained thumbnail is invalid") from exc
    if (
        not 12 <= len(data) <= MAX_RETAINED_THUMBNAIL_BYTES
        or data[:4] != b"RIFF"
        or data[8:12] != b"WEBP"
    ):
        raise ValueError("retained thumbnail is invalid")
    destination.write_bytes(data)


def build_artifacts(
    video: Path,
    pipeline: PipelineArtifacts,
    output_dir: Path,
    duration_seconds: float,
    *,
    edit_revision: int = 0,
    tracking_encoding: str = "gzip_v1",
    process_runner: Callable[..., Any] = subprocess.run,
) -> tuple[LocalArtifact, ...]:
    bvh, body, frame_count, warnings = _prepare_motion_outputs(
        pipeline, output_dir, tracking_encoding
    )
    thumbnail = output_dir / "thumbnail.webp"
    metadata_path = output_dir / "metadata.json"

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

    _write_metadata(
        metadata_path,
        pipeline.trace,
        frame_count,
        duration_seconds,
        edit_revision,
        sha256_file(video),
        bvh,
        body,
        thumbnail,
        warnings,
    )
    return _collect_artifacts(bvh, body, thumbnail, metadata_path)


def build_artifacts_without_source(
    pipeline: PipelineArtifacts,
    output_dir: Path,
    duration_seconds: float,
    *,
    retained_thumbnail: Path,
    retained_metadata: Path,
    edit_revision: int,
    tracking_encoding: str = "gzip_v1",
) -> tuple[LocalArtifact, ...]:
    """Rebuild corrected artifacts for a job whose source video no longer exists.

    The source hash is copied from the retained revision-0 metadata; it is never
    recomputed or fabricated, so corrected metadata keeps its original provenance.
    """
    source_sha256 = _retained_source_sha256(retained_metadata)
    bvh, body, frame_count, warnings = _prepare_motion_outputs(
        pipeline, output_dir, tracking_encoding
    )
    thumbnail = output_dir / "thumbnail.webp"
    _copy_retained_thumbnail(retained_thumbnail, thumbnail)
    metadata_path = output_dir / "metadata.json"
    _write_metadata(
        metadata_path,
        pipeline.trace,
        frame_count,
        duration_seconds,
        edit_revision,
        source_sha256,
        bvh,
        body,
        thumbnail,
        warnings,
    )
    return _collect_artifacts(bvh, body, thumbnail, metadata_path)
