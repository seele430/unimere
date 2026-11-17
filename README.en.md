# Unimere

> Many flows. One clear space.

[Showcase](https://seele430.github.io/) · [Demo video](docs/Personal-Agent-Demo.mp4) · [QQ setup (Chinese)](docs/qq-official.md) · [中文说明](README.md)

Unimere is a local workspace I built for my own use. It brings web chat, reminders, scheduled jobs, files, and QQ messages into one place. The backend uses FastAPI and SQLite; the frontend uses React.

The database, job history, and generated files are stored locally by default. Requests are sent to external services when using models, search, Edge TTS, and similar integrations.

## Features

- Chat: streaming responses, tools for reminders, sandbox file access, search, and image-to-image generation, with a record of tool calls.
- Bots: up to three bots, each with its own instructions, avatar, and voice.
- Reminders and jobs: scheduled reminders, in-app notifications, email and push channels, run history, and health checks.
- Official QQ bot: the official API v2 and outbound WebSocket gateway support allowlisted C2C chats, intent routing, task handoffs, and mixed text/image replies without a public IP address.
- Artifacts: registration, preview, download, version comparison, and SHA-256 hashes.
- Activity timeline: a read-only event stream for artifacts, job runs, and model calls.
- Usage and monitoring: model usage and budgets, site health checks, and alerts.
- Speech and video: Edge TTS, CPU transcription with faster-whisper, video summaries, and keyframe extraction.
- Image generation: a local ComfyUI integration with limits on VRAM, sampling steps, and concurrency.
- Platform console: node, sandbox, and execution views, with controls for creating sandboxes, submitting or cancelling runs, cordoning, and draining.

## Getting started

Requirements: Python 3.12, Node.js, and a DeepSeek API key.

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt

cd frontend
npm install
cd ..

# In data\.env, set: OPENAI_API_KEY=sk-xxxx

.\scripts\run-dev.ps1
```

The script starts the API on port 8000, the scheduler, the outbox worker, and the frontend on port 5173. Open <http://127.0.0.1:5173> and create the owner account on first launch.

To stop the services:

```powershell
.\scripts\run-dev.ps1 -Stop
```

The demo video was recorded with an earlier version, so some screens and features may differ from the current code.

## Usage boundaries

### Single user

Unimere is designed for one machine and one owner account. There is no public registration flow after account creation, and neither `/auth/register` nor `/auth/invite` is exposed.

Set `PERSONAL_AGENT_PLATFORM_TOKEN` before exposing the platform API. Reverse-proxy origin handling and trust settings are described in the [platform contract](docs/platform-contract.md).

### Read-only remote access

The remote guard authenticates requests before checking the HTTP method and path allowlist. Write methods are rejected. Phone access is intended for viewing artifacts and activity, not for performing desktop write operations.

See the [remote access threat model](docs/threat-model-remote.md) for self-signed TLS, one-time login links, lockouts, and log redaction. Direct TLS access has been tested on Android. nginx/Caddy reverse proxies, iOS certificate setup, and reconnection on unreliable networks have not been covered.

### Dry-run before deletion

Database records and files are handled by separate cleanup modules. Both generate a plan by default. Actual deletion requires explicit confirmation; on the CLI, `--apply` must be used together with `--confirm`.

### GPU safeguards

ComfyUI is started through `comfy-agent/scripts/start-comfyui-safe.ps1` with low-VRAM settings and a free-memory check. Image generation is limited to one image, at most 30 sampling steps, and a 120-second timeout. The service is stopped afterwards. Video transcription always uses the CPU.

These limits follow a `VIDEO_TDR_FAILURE` encountered on a machine with 6 GB of VRAM. See [GPU protection](docs/gpu-protection.md) for the configuration.

## Platform metrics

- HTTP 202 means that a request was accepted, not that it succeeded. Success is calculated from terminal states, and cancelled runs are excluded from the denominator.
- A `hold` decision does not mean that capacity changed.
- Actual capacity is based on valid leases in the `accepting` state, not on the number of node records.
- Live data, in-process samples, and What-if simulations are shown separately. Without a connected controller, calculated values do not imply that real capacity was changed.

## Tests

Backend tests run through a guarded runner. SQLite paths are redirected to a temporary directory, and network connections are restricted by a socket guard:

```powershell
.\scripts\test.ps1 backend

# List tests or audit isolation declarations
.\.venv\Scripts\python.exe scripts\run_tests.py list
.\.venv\Scripts\python.exe scripts\run_tests.py audit
```

Some mock provider tests require loopback connections and cannot run under the socket guard. They are run separately with their own logs and are not included in the guarded runner's pass count.

Frontend tests:

```powershell
cd frontend
npx playwright test
npx playwright test tests/console.spec.ts
```

Other checks:

```powershell
# End-to-end verification
.\.venv\Scripts\python.exe scripts\verification\verify_e2e.py

# GPU safeguards and documentation consistency
.\.venv\Scripts\python.exe scripts\verification\verify_gpu_guard.py
.\.venv\Scripts\python.exe scripts\verification\check_docs_consistency.py
```

## Project layout

```text
unimere/
├── src/                 # FastAPI, model calls, tools, and application modules
├── frontend/            # React + TypeScript frontend and Playwright tests
├── tests/               # Backend tests; see tests/registry.py
├── scripts/             # Startup, shutdown, test, backup, and verification scripts
├── comfy-agent/         # ComfyUI startup and invocation safeguards
├── docs/                # Guides, design notes, and historical archives
├── legacy/              # Retired code kept for reference
└── data/                # Local configuration, database, and logs; not versioned
```

## Known limitations

- In the existing keyword-based tests, faster-whisper large-v3 on the local CPU did not reach 95% accuracy on long audio. See [video transcription limits](docs/video-asr-limits.md).
- Scenario replay records scaling events, not every `hold` decision.
- Console commands are split on spaces without parsing quotes or escapes. Complex commands need to use the `/v1` API.
- This is a personal project. It does not provide multi-tenancy, mobile write access, or a complete public-deployment setup.

Earlier `/v1` authentication and rate-limit fixes are recorded in `docs/journal.md` and `docs/platform-contract.md`.
