from pathlib import Path
import json
import threading
import time
import os
import pytest

from maxmcp.rtmw3d.runtime import Rtmw3dReadiness
from maxmcp.rtmw3d.motion import BODY23_NAMES
from maxmcp.worker.api_client import (
    ClaimedJob,
    HeartbeatResult,
    UploadTarget,
    WorkerApiError,
)
from maxmcp.worker.artifacts import (
    MAX_TRACKING_COMPRESSED_BYTES,
    MAX_TRACKING_DECOMPRESSED_BYTES,
    LocalArtifact,
)
from maxmcp.worker.motion_pipeline import PipelineArtifacts, PipelineCancelled
from maxmcp.worker.runner import ArtokeWorker, RunResult


JOB_ID = "00000000-0000-4000-8000-000000000001"


def readiness(root: Path) -> Rtmw3dReadiness:
    return Rtmw3dReadiness(True, root / "env", root / "repo", root / "models", ())


class Api:
    def __init__(self, claim=True) -> None:
        self._claim = claim
        self.heartbeat_calls = 0
        self.published = None
        self.failed = None
        self.cancelled = False

    def claim(self):
        if not self._claim:
            return None
        return ClaimedJob(JOB_ID, "walk.mp4", "owner/job/source/walk.mp4", "https://signed", 4.0)

    def heartbeat(self, _job, _stage, _progress):
        self.heartbeat_calls += 1
        return HeartbeatResult(False, "later")

    def authorize_uploads(self, _job):
        return tuple(
            UploadTarget(kind, f"owner/job/result/{name}", "token", f"https://upload/{name}")
            for kind, name in [
                ("bvh", "motion.bvh"),
                ("rtmw3d_json", "motion.rtmw3d.json"),
                ("thumbnail", "thumbnail.webp"),
                ("metadata", "metadata.json"),
            ]
        )

    def publish(self, job, manifest, edit_revision=0):
        self.published = (job, manifest, edit_revision)

    def finish_failed(self, job, code):
        self.failed = (job, code)

    def finish_cancelled(self, _job):
        self.cancelled = True


def dependencies(tmp_path: Path):
    artifacts: list[LocalArtifact] = []

    def download(_url, destination, **_kwargs):
        destination.write_bytes(b"video")
        return destination

    def build(
        _video,
        _pipeline,
        output,
        duration_seconds,
        edit_revision=0,
        tracking_encoding="gzip_v1",
    ):
        for kind, name in [
            ("bvh", "motion.bvh"),
            ("rtmw3d_json", "motion.rtmw3d.json"),
            ("thumbnail", "thumbnail.webp"),
            ("metadata", "metadata.json"),
        ]:
            path = output / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(kind.encode())
            artifacts.append(LocalArtifact(kind, path, path.stat().st_size, "a" * 64))
        return tuple(artifacts)

    uploads = []

    def upload(target, artifact):
        uploads.append((target.kind, artifact.kind))

    return download, build, upload, uploads


def test_empty_queue_returns_idle(tmp_path: Path) -> None:
    worker = ArtokeWorker(Api(claim=False), lambda: readiness(tmp_path), tmp_path / "cache")
    assert worker.run_once() is RunResult.IDLE


def test_success_heartbeats_uploads_and_publishes(tmp_path: Path) -> None:
    api = Api()
    download, build, upload, uploads = dependencies(tmp_path)

    class Pipeline:
        def run(self, _video, workspace, on_stage, _cancelled):
            on_stage("extracting", 15)
            time.sleep(0.03)
            file = workspace / "internal"
            file.write_text("x", encoding="utf-8")
            return PipelineArtifacts(file, file, file, 1)

        def cancel(self):
            pass

    built_encoding = None

    def encoding_builder(*args, tracking_encoding, **kwargs):
        nonlocal built_encoding
        built_encoding = tracking_encoding
        return build(*args, tracking_encoding=tracking_encoding, **kwargs)

    worker = ArtokeWorker(
        api, lambda: readiness(tmp_path), tmp_path / "cache",
        pipeline_factory=lambda _report: Pipeline(),
        downloader=download, artifact_builder=encoding_builder, uploader=upload,
        heartbeat_interval=0.01,
    )

    assert worker.run_once() is RunResult.COMPLETED
    assert api.heartbeat_calls >= 1
    assert uploads == [(kind, kind) for kind in ("bvh", "rtmw3d_json", "thumbnail", "metadata")]
    assert api.published and api.published[0] == JOB_ID
    assert len(api.published[1]) == 4
    assert built_encoding == "identity"


def test_initial_gzip_job_forwards_server_encoding_to_artifact_builder(tmp_path: Path) -> None:
    api = Api()
    initial = api.claim()
    api.claim = lambda: ClaimedJob(
        initial.job_id,
        initial.source_filename,
        initial.object_path,
        initial.download_url,
        initial.duration_seconds,
        tracking_encoding="gzip_v1",
    )
    download, build, upload, _uploads = dependencies(tmp_path)
    built_encoding = None

    class Pipeline:
        def run(self, _video, workspace, *_args):
            path = workspace / "internal"
            path.write_text("x", encoding="utf-8")
            return PipelineArtifacts(path, path, path, 1)

        def cancel(self):
            pass

    def encoding_builder(*args, tracking_encoding, **kwargs):
        nonlocal built_encoding
        built_encoding = tracking_encoding
        return build(*args, tracking_encoding=tracking_encoding, **kwargs)

    worker = ArtokeWorker(
        api,
        lambda: readiness(tmp_path),
        tmp_path / "cache",
        pipeline_factory=lambda _report: Pipeline(),
        downloader=download,
        artifact_builder=encoding_builder,
        uploader=upload,
    )

    assert worker.run_once() is RunResult.COMPLETED
    assert built_encoding == "gzip_v1"


@pytest.mark.parametrize("boundary", ["authorize", "publish"])
def test_publication_409_is_lease_lost_without_terminal_failure(tmp_path: Path, boundary: str) -> None:
    api = Api()
    if boundary == "authorize":
        api.authorize_uploads = lambda _job: (_ for _ in ()).throw(WorkerApiError("conflict", status=409))
    else:
        api.publish = lambda *_args: (_ for _ in ()).throw(WorkerApiError("conflict", status=409))
    download, build, upload, _uploads = dependencies(tmp_path)
    class ImmediatePipeline:
        def run(self, _video, workspace, *_args):
            path = workspace / "internal"; path.write_text("x"); return PipelineArtifacts(path, path, path, 1)
        def cancel(self): pass
    worker = ArtokeWorker(api, lambda: readiness(tmp_path), tmp_path / "cache",
        pipeline_factory=lambda _report: ImmediatePipeline(), downloader=download,
        artifact_builder=build, uploader=upload)
    assert worker.run_once() is RunResult.LEASE_LOST
    assert api.failed is None


@pytest.mark.parametrize("tracking_encoding", ["identity", "gzip_v1"])
def test_correction_rebuild_skips_inference_and_publishes_exact_revision(
    tmp_path: Path,
    tracking_encoding: str,
) -> None:
    api = Api()
    api.claim = lambda: ClaimedJob(
        JOB_ID,
        "walk.mp4",
        "owner/job/source/walk.mp4",
        "https://signed/video",
        4.0,
        edit_revision=3,
        tracking_encoding=tracking_encoding,
        tracking_url="https://signed/tracking",
        edits_url="https://signed/edits",
    )
    downloaded: list[tuple[str, dict[str, int]]] = []
    source_payload = {
        "schema": "artoke.rtmw3d.v1",
        "source_video": "walk.mp4",
        "fps": 30,
        "image_size": {"width": 1920, "height": 1080},
        "frames": [{
            "index": 0,
            "keypoints": {
                joint: [float(index), float(index + 1), float(index + 2)]
                for index, joint in enumerate(BODY23_NAMES)
            },
            "image_keypoints": {
                joint: [float(index * 10), float(index * 5)]
                for index, joint in enumerate(BODY23_NAMES)
            },
            "scores": {joint: 0.9 for joint in BODY23_NAMES},
        }],
    }

    def download(url, destination, **kwargs):
        downloaded.append((url, kwargs))
        if url.endswith("/video"):
            destination.write_bytes(b"video")
        elif url.endswith("/tracking"):
            destination.write_text(json.dumps(source_payload), encoding="utf-8")
        else:
            destination.write_text(json.dumps({
                "imageEdits": [{
                    "frame": 0,
                    "joint": "left_wrist",
                    "x": 310.5,
                    "y": 205.0,
                    "state": "manual",
                }],
                "poseEdits": [{
                    "frame": 0,
                    "joint": "left_elbow",
                    "rotation": [0.0, 0.0, 0.0, 1.0],
                }],
            }), encoding="utf-8")
        return destination

    def convert(source, output):
        corrected = json.loads(source.read_text(encoding="utf-8"))
        assert corrected["frames"][0]["keypoints"]["left_wrist"][:2] == [310.5, -205.0]
        output.write_text(
            "HIERARCHY\nROOT Pelvis\nMOTION\nFrames: 1\nFrame Time: 0.0333333333\n",
            encoding="utf-8",
        )
        return 1

    pose_calls: list[tuple[Path, object, Path]] = []

    def apply_pose(source, pose_edits, output):
        pose_calls.append((source, pose_edits, output))
        assert source.name == "corrected.base.bvh"
        assert pose_edits == ({
            "frame": 0,
            "joint": "left_elbow",
            "rotation": [0.0, 0.0, 0.0, 1.0],
        },)
        output.write_text("FINAL BVH", encoding="utf-8")
        return output

    built_revision = None
    built_encoding = None
    _download, _build, upload, uploads = dependencies(tmp_path)

    def build(
        video,
        pipeline,
        output,
        duration_seconds,
        edit_revision=0,
        tracking_encoding="gzip_v1",
    ):
        nonlocal built_revision, built_encoding
        built_revision = edit_revision
        built_encoding = tracking_encoding
        return _build(
            video,
            pipeline,
            output,
            duration_seconds,
            edit_revision,
            tracking_encoding=tracking_encoding,
        )

    worker = ArtokeWorker(
        api,
        lambda: readiness(tmp_path),
        tmp_path / "cache",
        pipeline_factory=lambda _report: (_ for _ in ()).throw(
            AssertionError("rebuild must not start RTMW3D inference")
        ),
        downloader=download,
        artifact_builder=build,
        uploader=upload,
        converter=convert,
        pose_applier=apply_pose,
    )

    assert worker.run_once() is RunResult.COMPLETED
    assert downloaded == [
        ("https://signed/video", {}),
        ("https://signed/tracking", {
            "max_bytes": MAX_TRACKING_COMPRESSED_BYTES,
            "max_decompressed_json_bytes": MAX_TRACKING_DECOMPRESSED_BYTES,
        }),
        ("https://signed/edits", {}),
    ]
    assert built_revision == 3
    assert built_encoding == tracking_encoding
    assert len(pose_calls) == 1
    assert len(uploads) == 4
    assert api.published and api.published[2] == 3


def test_local_correction_rebuild_runs_without_a_source_download(tmp_path: Path) -> None:
    api = Api()
    api.claim = lambda: ClaimedJob(
        JOB_ID,
        "walk.mp4",
        None,
        None,
        4.0,
        edit_revision=3,
        tracking_encoding="gzip_v1",
        tracking_url="https://signed/tracking",
        edits_url="https://signed/edits",
        transport="local_ephemeral",
        thumbnail_url="https://signed/thumbnail",
        metadata_url="https://signed/metadata",
    )
    downloaded: list[tuple[str, dict[str, int]]] = []
    source_payload = {
        "schema": "artoke.rtmw3d.v1",
        "source_video": "walk.mp4",
        "fps": 30,
        "image_size": {"width": 1920, "height": 1080},
        "frames": [{
            "index": 0,
            "keypoints": {
                joint: [float(index), float(index + 1), float(index + 2)]
                for index, joint in enumerate(BODY23_NAMES)
            },
            "image_keypoints": {
                joint: [float(index * 10), float(index * 5)]
                for index, joint in enumerate(BODY23_NAMES)
            },
            "scores": {joint: 0.9 for joint in BODY23_NAMES},
        }],
    }

    def download(url, destination, **kwargs):
        downloaded.append((url, kwargs))
        if url.endswith("/tracking"):
            destination.write_text(json.dumps(source_payload), encoding="utf-8")
        elif url.endswith("/edits"):
            destination.write_text("[]", encoding="utf-8")
        elif url.endswith("/thumbnail"):
            destination.write_bytes(b"RIFF\x04\x00\x00\x00WEBP")
        else:
            destination.write_text(
                json.dumps({"sha256": {"source": "e" * 64}}),
                encoding="utf-8",
            )
        return destination

    def convert(_source, output):
        output.write_text(
            "HIERARCHY\nROOT Pelvis\nMOTION\nFrames: 1\nFrame Time: 0.0333333333\n",
            encoding="utf-8",
        )
        return 1

    def apply_pose(source, pose_edits, output):
        assert pose_edits == ()
        output.write_bytes(source.read_bytes())
        return output

    _download, build, upload, uploads = dependencies(tmp_path)
    source_free_calls: list[dict[str, object]] = []

    def source_free_build(
        pipeline,
        output,
        duration_seconds,
        *,
        retained_thumbnail,
        retained_metadata,
        edit_revision,
        tracking_encoding="gzip_v1",
    ):
        source_free_calls.append({
            "thumbnail_bytes": retained_thumbnail.read_bytes(),
            "metadata": retained_metadata,
            "edit_revision": edit_revision,
            "tracking_encoding": tracking_encoding,
        })
        return build(None, pipeline, output, duration_seconds, edit_revision, tracking_encoding=tracking_encoding)

    worker = ArtokeWorker(
        api,
        lambda: readiness(tmp_path),
        tmp_path / "cache",
        pipeline_factory=lambda _report: (_ for _ in ()).throw(
            AssertionError("local rebuild must not start RTMW3D inference")
        ),
        downloader=download,
        artifact_builder=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("local rebuild must not build artifacts from a source video")
        ),
        source_free_artifact_builder=source_free_build,
        uploader=upload,
        converter=convert,
        pose_applier=apply_pose,
    )

    assert worker.run_once() is RunResult.COMPLETED
    assert [url for url, _ in downloaded] == [
        "https://signed/tracking",
        "https://signed/edits",
        "https://signed/thumbnail",
        "https://signed/metadata",
    ]
    thumbnail_bounds = downloaded[2][1]
    metadata_bounds = downloaded[3][1]
    assert thumbnail_bounds["max_bytes"] == 5 * 1024 * 1024
    assert metadata_bounds["max_bytes"] == 1024 * 1024
    assert len(source_free_calls) == 1
    assert source_free_calls[0]["edit_revision"] == 3
    assert source_free_calls[0]["tracking_encoding"] == "gzip_v1"
    assert source_free_calls[0]["thumbnail_bytes"].startswith(b"RIFF")
    assert len(uploads) == 4
    assert api.published and api.published[2] == 3
    assert api.failed is None


def test_server_cancellation_terminates_pipeline(tmp_path: Path) -> None:
    api = Api()
    api.heartbeat = lambda *_args: HeartbeatResult(True, "later")
    download, build, upload, _ = dependencies(tmp_path)
    stopped = threading.Event()

    class Pipeline:
        def run(self, *_args):
            stopped.wait(1)
            raise PipelineCancelled()

        def cancel(self):
            stopped.set()

    worker = ArtokeWorker(
        api, lambda: readiness(tmp_path), tmp_path / "cache",
        pipeline_factory=lambda _report: Pipeline(),
        downloader=download, artifact_builder=build, uploader=upload,
        heartbeat_interval=0.01,
    )

    assert worker.run_once() is RunResult.CANCELLED
    assert api.cancelled is True


def test_pipeline_failure_is_reported_with_safe_code(tmp_path: Path) -> None:
    api = Api()
    download, build, upload, _ = dependencies(tmp_path)

    class Pipeline:
        def run(self, *_args):
            raise RuntimeError("private local path")

        def cancel(self):
            pass

    worker = ArtokeWorker(
        api, lambda: readiness(tmp_path), tmp_path / "cache",
        pipeline_factory=lambda _report: Pipeline(),
        downloader=download, artifact_builder=build, uploader=upload,
    )

    assert worker.run_once() is RunResult.FAILED
    assert api.failed == (JOB_ID, "pipeline_failed")


def test_lost_lease_aborts_without_terminal_overwrite(tmp_path: Path) -> None:
    api = Api()
    api.heartbeat = lambda *_args: (_ for _ in ()).throw(WorkerApiError("lost", status=409))
    download, build, upload, _ = dependencies(tmp_path)
    stopped = threading.Event()

    class Pipeline:
        def run(self, *_args):
            stopped.wait(1)
            raise PipelineCancelled()

        def cancel(self):
            stopped.set()

    worker = ArtokeWorker(
        api, lambda: readiness(tmp_path), tmp_path / "cache",
        pipeline_factory=lambda _report: Pipeline(),
        downloader=download, artifact_builder=build, uploader=upload,
        heartbeat_interval=0.01,
    )

    assert worker.run_once() is RunResult.LEASE_LOST
    assert api.failed is None and api.cancelled is False


def test_heartbeat_network_failure_does_not_fake_user_cancellation(tmp_path: Path) -> None:
    api = Api()
    api.heartbeat = lambda *_args: (_ for _ in ()).throw(WorkerApiError("offline"))
    download, build, upload, _ = dependencies(tmp_path)
    stopped = threading.Event()

    class Pipeline:
        def run(self, *_args):
            stopped.wait(1)
            raise PipelineCancelled()

        def cancel(self):
            stopped.set()

    worker = ArtokeWorker(
        api, lambda: readiness(tmp_path), tmp_path / "cache",
        pipeline_factory=lambda _report: Pipeline(),
        downloader=download, artifact_builder=build, uploader=upload,
        heartbeat_interval=0.01,
    )

    assert worker.run_once() is RunResult.LEASE_LOST
    assert api.failed is None and api.cancelled is False


def test_run_forever_uses_capped_error_backoff(tmp_path: Path) -> None:
    api = Api(claim=False)
    attempts = 0

    def claim():
        nonlocal attempts
        attempts += 1
        raise WorkerApiError("offline")

    api.claim = claim

    class Stop:
        delays: list[float] = []

        def is_set(self):
            return len(self.delays) >= 4

        def wait(self, delay):
            self.delays.append(delay)
            return len(self.delays) >= 4

    stop = Stop()
    worker = ArtokeWorker(api, lambda: readiness(tmp_path), tmp_path / "cache")

    worker.run_forever(stop)

    assert attempts == 4
    assert stop.delays == [5.0, 10.0, 20.0, 30.0]


def test_run_forever_cleans_stale_jobs_before_polling(tmp_path: Path) -> None:
    stale = tmp_path / "cache" / JOB_ID
    stale.mkdir(parents=True)
    old = time.time() - 90_000
    os.utime(stale, (old, old))
    stop = threading.Event()
    stop.set()
    worker = ArtokeWorker(Api(claim=False), lambda: readiness(tmp_path), tmp_path / "cache")

    worker.run_forever(stop)

    assert not stale.exists()


def test_worker_rejects_non_video_source_filename(tmp_path: Path) -> None:
    api = Api()
    claim = api.claim()
    api.claim = lambda: ClaimedJob(
        claim.job_id, "payload.exe", claim.object_path, claim.download_url, claim.duration_seconds,
    )
    worker = ArtokeWorker(api, lambda: readiness(tmp_path), tmp_path / "cache")

    assert worker.run_once() is RunResult.FAILED
    assert api.failed == (JOB_ID, "invalid_source_filename")
