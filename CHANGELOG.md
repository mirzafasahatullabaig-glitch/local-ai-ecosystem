# Changelog

What changed, newest first, and why. Versions are git tags. Everything up to
v2.8.0 built the base system (protocol, router, model gateway, MQTT, Pi agent,
TFT, touch, ESP firmware, voice loop); this log starts after that.

## Verified on 2026-10-23 (live hardware, no mocks)

**End to end — 13/13 pass**

| Check | Result |
|---|---|
| Typed question → LLM → answer mirrored to the Pi TFT | "2 + 2 = 4", 0.6 s |
| Panel streaming answer | route event, chunks, done |
| Room temperature (station asks the ESP's DHT11) | 30.2 °C, answered correctly |
| Pad 1 (beep + blink), pad 2 (read DHT) — ESP commands | received → accepted → executing → completed |
| Vision on an uploaded photo | qwen3-vl:2b, 11 s |
| Panel "Pi camera" button (Pi webcam → vision) | 11 s, exposure correct |
| Voice "What do you see?" → Whisper → Pi camera → vision | 14.6 s |
| Web mic (browser webm) → Whisper | transcribed correctly |
| Pad 3 hold → live preview on TFT → release → snapshot | snapshot written, exposed |

**Failure and recovery — 11/13 pass** (`scripts/check_recovery.py`)

| Check | Result |
|---|---|
| Pi agent killed (crash) → marked offline | 0.9 s |
| … restarts by itself and is usable again | 5.0 s |
| Malformed commands (bad JSON, missing fields, empty id), Pi and ESP | rejected, device keeps working |
| Unknown action, Pi and ESP | `failed`, never faked |
| Stale command (issued 90 s ago, expired 60 s ago), Pi | `received → expired`, not run |
| Same command id sent twice, Pi | second one ignored |
| Station restart → both devices live again, LLM answers | 8 s |
| **Stale command, ESP** | **runs it** — fixed in v2.18.0 firmware, needs a USB reflash |
| **Same command id twice, ESP** | **runs twice** — fixed in v2.18.0 firmware, needs a USB reflash |

Not yet tested: broker restart, Wi-Fi loss, Pi power loss (need sudo / hands on hardware).

**Security** — from the LAN address: every read endpoint returns 401 without
the key, wrong keys are refused, the key is never in the page served to the
LAN; Ollama answers only on 127.0.0.1 (unreachable from the Pi); the broker
refuses anonymous and wrong-password clients. Still open: MQTT is not
encrypted on the LAN.

## v2.18.0 — commands run once, and never late
- Pi agent ignores a command id it has already run (QoS 1 can redeliver).
- ESP firmware 0.5: NTP wall clock; refuses commands past `expires_at`;
  ignores repeated command ids; timestamps are real epoch ms. This also fixes
  a latent bug: the ESP's over-33 °C alert to the Pi used boot-relative time,
  so the Pi would have dropped every alert as expired. **Needs a USB reflash.**
- `scripts/check_recovery.py`: the failure/recovery check above.

## v2.17.0 — developer page, security pass
- `/dev`: every LLM call with its exact prompt, routing and timings; live MQTT
  traffic and a raw command sender (only actions a device advertises); station
  and Pi logs; laptop CPU/RAM/GPU and Pi health; device manifests.
- `/docs` got an Authorize button; `/ask` accepts a forced `role` for testing.
- `/devices`, `/models`, `/tasks` now need the API key (they were readable by
  anyone on the WiFi — `/tasks` is the question history).
- Panel and dev page send a Content-Security-Policy (`connect-src 'self'`).
- README rewritten to match what is actually built.

## v2.16 — pad 3 live camera
- Hold pad 3: the TFT shows the USB webcam live. Release: the frame is sent
  to the vision model and the answer appears on the TFT.
- Webcam capture waits 30 frames for exposure (10 gave black shots).
- Touch and TFT services now log to the journal (their output was lost).

## v2.15.0 — touch pads
- Pad 1 beep + blink, pad 2 temperature, pad 3 vision, pad 4 voice.

## v2.14.0 — vision and web speech
- Photos into the vision model from: panel upload (phone camera), laptop
  webcam, Pi USB webcam, and voice ("what do you see").
- Mic button in the panel (browser recording → Whisper).

## v2.13.0 — smarter model switching
- No more 12–40 s model swaps for everyday phrases ("let me see…", "and
  then…"); the panel shows which model is about to run.

## v2.12 — web panel
- Live face, room temperature, device buttons, event log, streaming answers.
- The API key was embedded in the page for anyone on the LAN — now only for
  the laptop. Station logs now actually reach `api.log`.
- Fixed: the Ask button did nothing (inline handler name clash).
- Room facts added only to room questions (the model commented on them).

## v2.11.0 — laptop brain
- Room questions fetch a fresh DHT reading; answer "28.5 °C" instead of "I
  can't sense temperature".
- Voice no longer freezes the server; answers reach the TFT without waiting.
- `scripts/start-station.bat` + Startup shortcut: the station starts at logon.
- Speech-to-text defaults to CPU (GPU path failed with a cuBLAS error).

## v2.10 — Pi TFT bot face
- Animated face (idle / talking / alert / offline), device list with
  last-seen age, answer band, temperature; message fades after 90 s.
- Answers trimmed to fit; no overlap with the temperature band.
- Screen push built by Pillow instead of a per-pixel Python loop.

## v2.9 — emoji and ESP clean-up
- Colour emoji on the TFT, ASCII icons on the ESP OLED.
- ESP sketch: restored missing declarations; the real `.ino` with WiFi/MQTT
  credentials is gitignored, `.ino.example` is tracked.
