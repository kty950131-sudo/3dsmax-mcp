# ARTOKE NVIDIA worker on Windows

## Requirements

- Windows with a supported NVIDIA GPU and working CUDA driver
- Python 3.12 environment used by the RTMW3D installation
- RTMW3D repository, checkpoint, and runtime reported as ready by the existing readiness check
- `ffmpeg.exe` available on `PATH`
- ARTOKE worker token issued by the ARTOKE server administrator

The worker does not require 3ds Max to be open. It processes one GPU job at a time.

## Configure the token

Run this in a normal PowerShell window from the repository root:

```powershell
.\scripts\set_artoke_worker_token.ps1
```

Windows prompts for the token without placing it in the PowerShell command history. The token is stored as the generic credential `ARTOKE/MotionWorkerToken`. Do not paste it into `.env`, a shortcut, a URL, or a log.

## Check readiness

```powershell
python -m src.worker doctor
```

Both RTMW3D and ffmpeg must report ready before the worker can claim a website job. Doctor performs local checks only and does not contact ARTOKE or claim work.

## Run interactively

```powershell
python -m src.worker run --url https://artoke.com
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
python -m src.worker cleanup
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
