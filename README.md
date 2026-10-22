# local-ai-ecosystem — Phase 1

Local-first AI robotics backend. Phase 1 is the brain and plumbing only:
a protocol, a device registry, a request router, a model gateway over Ollama,
and an HTTP API. **No messaging layer, no devices, no sensors yet** — anything
that needs to reach a device returns an explicit "Phase 2" error instead of
pretending to succeed.

Target machine: Windows, 24 GB RAM, one NVIDIA GPU (~8 GB VRAM), Ollama,
PowerShell.

## Layout

```
backend/
  api/main.py              FastAPI app (create_app factory)
  orchestrator/router.py   deterministic Tier-1 regex + role classifier
  model_gateway/gateway.py Ollama calls, VRAM discipline, fallback, metrics
  device_registry/registry.py  register/heartbeat/stale/find-by-capability
  storage/sqlite_repo.py   thread-safe SQLite (devices, tasks)
schemas/messages.py        protocol v1: Envelope/Manifest/Command/Event
scripts/                   free_gpu.ps1, verify_models.ps1, bench_models.py
tests/                     all mocked — no real Ollama in pytest
```

## Setup

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1          # if blocked: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and adjust if needed. Everything runs inside the
activated `.venv` — check with `where python` (must point into `.venv`).

### Ollama settings (set once, then restart Ollama)

User environment variables:
- `OLLAMA_MAX_LOADED_MODELS=1`
- `OLLAMA_NUM_PARALLEL=1`
- `OLLAMA_HOST=127.0.0.1:11434`  (loopback only — never `0.0.0.0`)

## Running

```powershell
.\scripts\free_gpu.ps1                 # always free the GPU first
.\scripts\start-station.bat            # loads .env.mqtt, serves :8000 for the Pi, logs to api.log
```

`start-station.bat` also runs at logon (shortcut "eco station" in the Windows
Startup folder) and exits if :8000 is already taken, so it never starts twice.
Speech-to-text runs on CPU (`ECO_STT_DEVICE=cpu` default); set it to `cuda`
once cuBLAS is set up.

Endpoints: `GET /health`, `GET /models`, `GET /devices`,
`POST /devices/register`, `POST /ask`, `POST /listen`, `GET /tasks`.
Room questions ("what's the temperature") make the station ask the ESP for a
fresh DHT reading and give it to the LLM; `/listen` answers are kept short.

`POST /ask` examples:
- `{"text":"list devices"}` → deterministic, no model call.
- `{"text":"what is MQTT"}` → routed to a model, returns the answer + metrics.
- `{"text":"estop"}` → HTTP 501, "requires messaging layer (Phase 2)".

## Roles and models

| Role | Primary | Fallback |
|------|---------|----------|
| reason | qwen3.5:4b | llama3.2:3b |
| vision | qwen3.5:4b* | qwen3-vl:2b |
| code | qwen2.5-coder:7b | deepseek-coder:6.7b |
| plan | qwen3.5:4b | llama3.2:3b |

\* The text primary does not accept images on this Ollama build
(`verify_models.ps1` reports NO), so vision requests route straight to
`qwen3-vl:2b`. Only the primary stays warm (`keep_alive=-1`); a specialist
unloads the others first, then the primary is re-warmed.

## Testing

```powershell
pytest -q
```

All tests use `httpx.MockTransport` and `tmp_path` — no real Ollama calls.

## Verifying models and benchmarking

```powershell
.\scripts\verify_models.ps1            # confirms models, probes image support
python scripts\bench_models.py --runs 5
```

`bench_models.py` frees the GPU before each cold run, measures cold vs warm, a
vision call, and 1 vs 2 concurrent requests, and writes median + p95 to
`bench_results/*.csv` along with the `ollama ps` GPU/CPU split.

Measured on this machine (RTX 5050 Laptop, 3 runs each, 100% GPU, no CPU spill):

| scenario | model | median | p95 |
|----------|-------|--------|-----|
| cold primary | qwen3.5:4b | 12.6 s | 12.6 s |
| warm primary | qwen3.5:4b | 4.3 s | 7.1 s |
| vision | qwen3-vl:2b | 1.1 s | 7.3 s |
| concurrent (1) | qwen3.5:4b | 7.2 s/req | 9.2 s |
| concurrent (2) | qwen3.5:4b | 9.4 s/req | 15.4 s |

qwen3.5:4b is a reasoning model, so per-answer latency is high (it emits a long
thinking trace). These are real numbers from one short run, not a tuned
benchmark.

## Not in Phase 1 (deferred)

MQTT, the Pi agent, ESP32 firmware, the dashboard, auth, Tailscale, and
**sensors entirely**. The LLM never generates code or commands that run on
devices.
