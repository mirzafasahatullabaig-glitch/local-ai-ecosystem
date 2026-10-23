# Security

How this project is secured, and the checks that verify it. All checks below
were run live against the running system on 2026-10-23; re-run them any time with
the commands shown. Current commit at last verification: `b8616c9`.

## Threat model

A small LAN system: a Windows laptop (the station), a Raspberry Pi (MQTT broker
+ devices), an ESP32. The assumptions:

- The LAN is semi-trusted — other people may be on the same Wi-Fi.
- The API and devices are **not** exposed to the internet (no port-forwarding).
- The LLM can be wrong or manipulated, so it must never control hardware directly.

## Controls and evidence

### 1. Every API endpoint is authenticated (except liveness)

Each request needs a secret `X-API-Key` header. `/health` (liveness only,
reveals nothing) and `/docs` are the sole exceptions. Verified from the LAN
address, i.e. what another machine on the Wi-Fi sees:

| Request (no key) | Result |
|---|---|
| `GET /devices /models /tasks /status` | **401** |
| `GET /dev/state /dev/health /dev/logs` | **401** |
| `POST /ask`, `POST /dev/command` | **401** |
| wrong key on `/devices` | **401** |
| correct key on `/devices` | 200 |
| `GET /health` | 200 (by design) |

Check: `curl -s -o /dev/null -w '%{http_code}' http://<laptop>:8000/devices`

### 2. The API key is never exposed to other machines

The key is embedded in the page only when it is opened from the laptop itself
(127.0.0.1); any other browser must enter it, and it is kept in that browser's
local storage. Both pages send a Content-Security-Policy with
`connect-src 'self'`, so even injected script cannot send the key elsewhere.
Verified: the key appears **0** times in the page served to a LAN browser.
Tests: `test_pages_send_csp_and_hide_key_from_lan`, `test_panel_key_only_embedded_for_loopback`.

### 3. The local LLM is not reachable from the network

Ollama binds to `127.0.0.1:11434` only. Verified: reachable from the laptop
(200), refused/timeout from the LAN address; listening socket is `127.0.0.1:11434`.

### 4. The MQTT broker requires a password

Mosquitto uses password auth; anonymous and wrong-password clients are refused.
(Verified live earlier on 2026-10-23; the Pi was asleep at the final run — re-check
with `mosquitto_sub -h 127.0.0.1 -t 'eco/#' -C 1` on the Pi: it must print
"not authorised".) **Known limitation:** MQTT traffic is not encrypted, so a
sniffer on the same Wi-Fi could read the broker password. Acceptable for a
trusted home LAN; add TLS (port 8883) before an untrusted network.

### 5. The LLM can never issue a hardware command

Device commands come only from a fixed, server-side list — never from LLM
output. The web panel can send just: `ping, blink_led, beep, read_dht,
tft_show, oled_show`, with parameters chosen by the server. The developer page
can send only actions a device advertises in its own manifest, with a 2 KB
parameter cap. `update` (device self-update) and `estop` are not reachable
from either page. Grep confirms no path turns `gateway` output into a command.
Tests: `test_panel_action_whitelist_and_capability_checks`,
`test_raw_command_only_advertised_actions`, `test_forced_role_overrides_router`.

### 6. Commands expire and cannot be replayed

Every command carries `expires_at` (rejected if `<= issued_at`); devices
refuse stale commands and ignore a command id they have already run. Verified
live: a stale command to the Pi returns `received -> expired`; a duplicate is
ignored. Tests: `test_is_expired`. (ESP enforcement ships in firmware 0.5,
pending a USB reflash.)

### 7. No secrets or personal information in git

Passwords, API key, Wi-Fi name, LAN addresses and usernames live only in the
gitignored `.env.mqtt` and the gitignored filled-in `.ino`; tracked files use
placeholders. Verified across the **entire** commit history: the Wi-Fi
password, MQTT password, API key, LAN IPs and the Pi username each appear **0**
times. Commits use a GitHub no-reply email, not a personal one.
Check: `git log --all -p | grep -c <value>`

### 8. Dependencies have no known vulnerabilities

`pip-audit` over the installed environment: **No known vulnerabilities found**
(after upgrading `setuptools`, which had flagged CVEs).
Check: `.venv\Scripts\python -m pip_audit`

### 9. The test suite covers the security behaviour

`pytest -q` -> **100 passed**. The checks above are pinned by named tests so a
regression fails the build, not just a manual probe.

## What is NOT claimed

- Not audited by a third party; this is self-verification with reproducible checks.
- MQTT is unencrypted on the LAN (see #4).
- No e-stop exists on any device — fine today (no motors); a hardware kill
  switch and a device-side watchdog are required before any actuator is added.
- Failure cases still untested: broker restart, Wi-Fi loss, Pi power loss.

## Reproduce everything

```powershell
.venv\Scripts\python -m pytest -q
.venv\Scripts\python -m pip_audit
.venv\Scripts\python scripts\check_recovery.py   # live; disrupts devices on purpose
```
Plus the `curl` probes under each control above.
