# ARTOKE NVIDIA worker on Windows

## Requirements

- Windows with a supported NVIDIA GPU and working CUDA driver
- Python 3.12 environment used by the RTMW3D installation
- RTMW3D repository, checkpoint, and runtime reported as ready by the existing readiness check
- `ffmpeg.exe` available on `PATH`
- ARTOKE worker token issued by the ARTOKE server administrator

The worker does not require 3ds Max to be open. It processes one GPU job at a time.

## Installation scope

The active ARTOKE worker and RTMW3D runtime are supported only from a configured repository checkout. Installing the `3dsmax-mcp` wheel alone is not sufficient: it does not include, create, or configure `.venv-rtmw3d`, the `vendor` extractor repositories, or the model/checkpoint files. Run the commands below from the checkout where `scripts/install-rtmw3d.ps1` has configured those external runtime assets.

## Configure the token

Run this in a normal PowerShell window from the repository root:

```powershell
.\scripts\set_artoke_worker_token.ps1
```

Windows prompts for the token without placing it in the PowerShell command history. The token is stored as the generic credential `ARTOKE/MotionWorkerToken`. Do not paste it into `.env`, a shortcut, a URL, or a log.

## Check readiness

```powershell
python -m maxmcp.worker doctor
```

Both RTMW3D and ffmpeg must report ready before the worker can claim a website job. Doctor performs local checks only and does not contact ARTOKE or claim work.

## Run interactively

```powershell
python -m maxmcp.worker run --url https://artoke.com
```

Press `Ctrl+C` to stop. An interrupted active lease expires after 90 seconds and can then be claimed again.

## Start with Windows login

```powershell
.\scripts\register_artoke_worker.ps1
```

This writes only the `ARTOKE Motion Worker` value under the current user's `HKCU` Run key. It uses absolute Python, launcher, and repository paths and opens no console window. Moving the repository or Python environment requires registering again.

Disable startup without deleting credentials, results, or project files:

```powershell
.\scripts\unregister_artoke_worker.ps1
```

## Clean abandoned local jobs

```powershell
python -m maxmcp.worker cleanup
```

Only expired UUID-named directories below the ARTOKE worker cache are removed. Current work and unrelated directories are retained. Successful, failed, and cancelled jobs clean their local source and results automatically.

## Result flow

1. The worker atomically claims one queued ARTOKE job.
2. It downloads the owner's private source through a signed URL.
3. RTMW3D produces joint JSON and the converter produces a 30 FPS BVH.
4. ffmpeg produces a midpoint WebP thumbnail and the worker writes metadata.
5. The four fixed artifacts are uploaded with short-lived signed URLs.
6. ARTOKE verifies each path, size, and SHA-256 before completing the job.

The local worker never receives a Supabase service-role key or anon key. Tokens and signed URL query strings must not be copied into support logs.

## Corrected BVH rebuilds

An initial claim has `editRevision: 0` and null tracking/edit URLs. A correction claim has a positive revision plus `trackingUrl` and `editsUrl`. The worker downloads the source, immutable original RTMW3D JSON, and exact edit snapshot; validates BODY23 corrections; writes through a unique same-directory temporary file; skips inference; and converts corrected JSON directly to BVH.

Metadata and the publication manifest carry the claimed revision. Corrected artifacts upload beneath `result/revisions/<revision>/`; ARTOKE rejects any claimed, requested, manifest, or path revision mismatch. Original JSON and BVH remain at their fixed `result/` paths.

## Operational recovery

- HTTP `409` during heartbeat or publication means the lease or revision is no longer valid. Stop and allow normal cleanup/reclaim.
- Network retries are bounded by the usable lease. Cancellation uses the same cleanup path for initial and correction jobs.
- `python -m maxmcp.worker cleanup` removes only expired UUID workspaces below the configured cache.
- Logs may include job IDs, normalized stages, and safe codes. Never log bearer tokens, credential contents, source media, correction coordinates, object paths, or signed query strings.

## Member smoke-test checklist

With a locally owned MP4 shorter than 30 seconds, verify upload through completion, automatic editor opening, a persisted wrist edit after reload, corrected publication, separate original/corrected downloads, corrected BVH import in 3ds Max, and complete job deletion. This operator test requires a configured server, private storage, member account, and ready NVIDIA/RTMW3D machine; `doctor` does not perform it.
