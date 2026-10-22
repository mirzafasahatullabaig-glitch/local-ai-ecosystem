# local-ai-ecosystem

Local-first AI station for a small set of devices. A Windows laptop runs the
"brain" (LLM, speech-to-text, vision, web panel); a Raspberry Pi runs the MQTT
broker, a TFT face/status screen, touch pads, a mic and a USB webcam; an ESP32
drives a DHT11, an OLED and a buzzer. Everything is on the LAN, nothing goes to
the cloud.

```
            browser (panel / dev page)
                    │ HTTP :8000 (API key)
   ┌────────────────▼─────────────────┐        ┌─────────────────────────────┐
   │ laptop — the station             │  MQTT  │ Raspberry Pi                │
   │ FastAPI · Ollama (127.0.0.1)     │◄──────►│ Mosquitto (password auth)   │
   │ faster-whisper · router · panel  │ :1883  │ agent · TFT · touch · mic   │
   └──────────────────────────────────┘        │ USB webcam                  │
                    ▲  HTTP /listen, /ask      └──────────────┬──────────────┘
                    └─────────── Pi voice + pad 3 ────────────┘ MQTT
                                                     ┌────────▼────────┐
                                                     │ ESP32 dev module │
                                                     │ DHT11 OLED buzzer│
                                                     └──────────────────┘
```

## Status

### What is built

| Area | What works |
|---|---|
| **Station (laptop)** | FastAPI on :8000, starts at logon; local LLMs via Ollama with automatic model choice per question (reason / code / plan / vision); Whisper speech-to-text; answers mirrored to the Pi screen |
| **Room awareness** | Asking about the temperature makes the station read the ESP's DHT11 and answer with the real value |
| **Voice** | Pi pad 4 push-to-talk, and a mic button in the web panel |
| **Vision** | Questions about a photo from: panel upload (phone camera), laptop webcam, the Pi's USB webcam, voice ("what do you see"), or Pi pad 3 (hold for live preview, release to snap) |
| **Web panel** | Bot face, room temperature, device buttons, live event log, streaming answers, photo/webcam/mic |
| **Developer page** | Every LLM call with its exact prompt and timings, live MQTT traffic, raw command sender, station and Pi logs, laptop/Pi health |
| **Pi** | Animated bot face on the TFT with device list, answers and temperature; 4 touch pads; mic; USB webcam; self-restarting services |
| **ESP32** | DHT11, OLED status screen, buzzer, LED. Firmware 0.5 in this repo adds over-the-air updates, an over-33 °C alert to the Pi, NTP time and stale/duplicate command checks — the board runs an older version until it is reflashed |
| **Safety** | The LLM never sends device commands; fixed whitelists; commands expire and report every step; duplicates run once (Pi) |
| **Security** | API key on every endpoint except `/health`; key never sent to LAN browsers; page CSP; Ollama loopback-only; broker password auth; no secrets or personal info in git |

### Verified on real hardware (2026-10-23)

| Check | Result |
|---|---|
| End to end: text, streaming, TFT mirror, room temperature, pads 1–3, vision ×4 routes, web mic | **13 / 13 pass** |
| Pi agent crash → offline / back online by itself | 0.9 s / 5.0 s |
| Station restart → both devices live again | 8 s |
| Malformed, unknown, stale and duplicate commands | rejected or ignored on the Pi; ESP needs the 0.5 reflash (below) |
| API from another LAN machine without the key | every endpoint 401 except `/health` |
| Ollama reachable from the Pi | no (loopback only) |
| Broker with no / wrong password | refused |
| Unit tests | 100 pass |

Re-run with `pytest -q` and `scripts\check_recovery.py`; full per-version history and results in [CHANGELOG.md](CHANGELOG.md).

### Open

- **Reflash the ESP over USB** (firmware 0.5: NTP clock, refuses stale and duplicate commands, fixes the over-33 °C alert). Not compiled on the station — no `arduino-cli` there.
- **No e-stop on any device** — fine today (no motors), required before any actuator (see Safety model).
- **MQTT is not encrypted** on the LAN.
- **Untested:** broker restart, Wi-Fi loss, Pi power loss.
- Vision and code questions take 10–20 s because only one model fits in GPU memory at a time.

## Layout

```
backend/
  api/main.py            FastAPI app (create_app factory): /ask, /ask/stream, /listen, panel
  api/dev.py, dev.html   developer page: LLM traces, MQTT traffic, logs, health
  api/panel.html         web panel
  orchestrator/router.py deterministic regex tier + LLM role classifier
  model_gateway/         Ollama calls, one-model-in-VRAM, fallback, streaming, images
  messaging/bus.py       MQTT command bus + DeviceListener (registry, sensors, images, traffic)
  device_registry/       register / heartbeat / stale / find-by-capability
  storage/sqlite_repo.py devices and task history
schemas/messages.py      protocol v1 (see "Device protocol")
devices/pi/              pi_agent.py, tft_status.py, touch_ttp224.py, voice.py
devices/esp32_dev/       eco_esp32_dev.ino.example (copy to .ino, add credentials)
scripts/                 start-station.bat, free_gpu.ps1, verify_models.ps1, bench_models.py
tests/                   all mocked: no real Ollama, broker or hardware in pytest
```

## Running

```powershell
py -3.11 -m venv .venv; .\.venv\Scripts\Activate.ps1; pip install -r requirements.txt
.\scripts\start-station.bat     # loads .env.mqtt, serves :8000, logs to api.log, one instance only
```

`start-station.bat` also runs at logon (shortcut "eco station" in the Startup
folder). `.env.mqtt` (gitignored) holds `ECO_MQTT_USER`, `ECO_MQTT_PASS`,
`ECO_API_KEY`, `ECO_BROKER` (the Pi's address — the station won't start
without it) and `ECO_PI_SSH` (`user@pi`, for the developer page's logs and
health); see `.env.example`. Ollama user env: `OLLAMA_HOST=127.0.0.1:11434` (loopback only),
`OLLAMA_MAX_LOADED_MODELS=1`, `OLLAMA_NUM_PARALLEL=1`.

- **Panel** — `http://localhost:8000/`: bot face, room temperature, devices and
  their buttons, streaming answers, photo / webcam / Pi camera, mic.
- **Developer page** — `http://localhost:8000/dev`: every LLM call with the
  exact prompt and timings, live MQTT traffic, raw command sender, logs of the
  station and Pi services, laptop and Pi health.
- **API docs** — `http://localhost:8000/docs` (press Authorize, paste the key).

Webcam and mic in the browser need `http://localhost` (browsers block them on a
plain-http LAN address); from a phone use the Photo button.

Pi services (systemd, secrets in root-only `/etc/eco/eco.env`): `eco-agent`,
`eco-tft`, `eco-touch`. Code is deployed by copying `devices/pi/*.py` to
`~/eco-agent/` and restarting the service.

### Touch pads (TTP224 on GPIO 5, 6, 13, 16)

| Pad | Does |
|---|---|
| 1 | ESP beep + blink LED |
| 2 | ESP reads the DHT11 (shown on OLED, TFT, panel) |
| 3 | hold: live webcam on the TFT · release: snapshot → vision model → answer on the TFT |
| 4 | push-to-talk: 4 s recording → Whisper → LLM → answer on the TFT |

## How a request is handled

1. `router.route()` — anchored regexes for fixed actions (`list devices`,
   `ping`, `estop`, …) with **no LLM involved**; otherwise a role:
   `code` / `plan` / `vision` / `reason`.
2. An attached image (or the Pi camera) means `vision`. Without one, `vision`
   falls back to `reason` (a swap would cost ~12 s for nothing).
3. Room questions trigger a fresh `read_dht` and add the reading to the prompt.
4. The gateway calls Ollama, falling back to the role's second model on error,
   and keeps only one model in VRAM.
5. The answer is trimmed to 110 chars and mirrored to the Pi TFT.

| Role | Model | Fallback | Typical |
|---|---|---|---|
| reason | llama3.2:3b (kept loaded) | qwen3.5:4b | 0.3–1 s |
| code | qwen2.5-coder:7b | deepseek-coder:6.7b | ~14–20 s (model swap) |
| plan | qwen3.5:4b | llama3.2:3b | ~40 s (swap + reasoning trace) |
| vision | qwen3-vl:2b | — | ~6–12 s (swap) |

## Safety model

- **The LLM never produces commands.** Device commands come only from the
  regex tier, the panel's fixed server-side whitelist (`ping`, `blink_led`,
  `beep`, `read_dht`, `tft_show`, `oled_show`, with server-chosen params), or
  the developer page, which can only send actions a device advertises in its
  manifest (params capped at 2 KB).
- **Commands expire** (`expires_at`, 30 s) and devices report every step:
  `received → accepted → executing → completed | failed | rejected | expired`.
  The panel shows those states, not just "sent".
- **E-stop is not implemented on any device.** `eco/v1/system/estop` is
  reserved in the protocol, and `estop` returns "no live device offers
  capability 'estop'" rather than a fake success. Today's devices have no
  motors (LEDs, buzzer, screens, camera). **Before adding any actuator:** a
  hardware kill switch that cuts motor power, plus a device-side watchdog that
  stops the motor on its own if it stops hearing from the station — neither may
  depend on the laptop, the network or a model.

## Security

| Surface | State |
|---|---|
| HTTP API | Every endpoint except `/health` and `/docs` requires `X-API-Key`. |
| Panel / dev page | Key embedded only when opened from the laptop itself; other browsers enter it once (localStorage). Both pages send a CSP with `connect-src 'self'`, so even injected script can't send the key elsewhere. |
| Ollama | Bound to `127.0.0.1`; unreachable from the LAN (verified from the Pi). |
| MQTT | Password auth; anonymous and wrong-password clients are refused (verified). **Not encrypted**: someone on the same WiFi could sniff the password. TLS on 8883 is the fix if the LAN isn't trusted. |
| Secrets and personal info | `.env.mqtt` and the filled-in ESP `.ino` are gitignored; only `.ino.example` is tracked. Addresses, usernames and passwords live only there; tracked files use placeholders (`YOUR_PI_IP`). Commits use the GitHub no-reply email. |
| Remote access | None is set up. Do not port-forward :8000 or :1883; a VPN (e.g. Tailscale) would still need the API key. |

## Device protocol (v1)

Topics: `eco/v1/devices/<device_id>/{manifest,state,cmd,evt}`.
Messages are defined in `schemas/messages.py`; every one carries
`schema_version`, `msg_id`, `device_id` (`^[a-z0-9_]{3,32}$`) and `ts`.

- **manifest** (retained) — `device_type`, `hw`, `fw_version`,
  `protocol_version: 1`, `capabilities[]` each with `name`, `params`,
  `max_duration_ms`, `physical`, `risk: none|low|medium|high`.
- **state** (retained) — `{"online": true}` every 5 s; the broker's last-will
  publishes `{"online": false}` when a device drops. No heartbeat for 15 s =
  stale.
- **cmd** — `command_id`, `action`, `params`, `issued_at`, `expires_at`,
  `seq`, `origin.user`.
- **evt** — `command_id` + `state`; a result may add fields alongside
  (`temperature_c`, `humidity`, `image_b64`).

A new device: publish a manifest, heartbeat on `state`, subscribe to `cmd`,
answer each command with the event chain. `devices/pi/pi_agent.py` (Python)
and `devices/esp32_dev/eco_esp32_dev.ino.example` (Arduino) are reference
implementations.

## Testing

```powershell
pytest -q                                   # 100 tests, all mocked
.venv\Scripts\python scripts\check_recovery.py   # LIVE: kills the Pi agent, restarts the station
```

Results are summarised under [Status](#status); every version is in
[CHANGELOG.md](CHANGELOG.md).

## Benchmarks

`python scripts\bench_models.py --runs 5` measures cold/warm/vision/concurrent
latency into `bench_results/`. One short run on this RTX 5050 laptop: qwen3.5:4b
12.6 s cold / 4.3 s warm, qwen3-vl:2b 1.1 s warm. Treat as indicative.
