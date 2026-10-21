# Device agents (Phase 2)

Device-side code that speaks the Phase 1 protocol (`schemas/messages.py`) over
MQTT. Both agents connect to a Mosquitto broker on the Pi, announce a Manifest,
subscribe to their `cmd` topic, and emit `received→accepted→executing→
completed` Events.

## Broker (on the Pi)

```bash
sudo apt-get install -y mosquitto mosquitto-clients
# LAN listener so the laptop can reach it:
sudo tee /etc/mosquitto/conf.d/eco.conf >/dev/null <<'EOF'
listener 1883 0.0.0.0
allow_anonymous true
EOF
sudo systemctl restart mosquitto
```

> shortcut: `allow_anonymous` + `0.0.0.0` is open to the whole LAN, fine for a
> trusted bench network only. Add a password + ACL before anything else.

## Pi agent (`pi/pi_agent.py`)

Needs a venv with `paho-mqtt` and `pydantic`, plus `schemas/messages.py` copied
next to it as `messages.py`:

```bash
python3 -m venv ~/eco-agent/.venv
~/eco-agent/.venv/bin/pip install paho-mqtt pydantic
# copy schemas/messages.py -> ~/eco-agent/messages.py and pi_agent.py -> ~/eco-agent/
~/eco-agent/.venv/bin/python ~/eco-agent/pi_agent.py
```

## ESP32-S3 (`esp32_s3/eco_esp32_s3.ino`)

Arduino IDE, board **ESP32S3 Dev Module**, libraries **PubSubClient** and
**ArduinoJson v7**. Fill in the WiFi/broker constants at the top, flash, watch
Serial at 115200. LED is GPIO 48 (change for your board).

> shortcut: ESP uses boot-relative `ts` (millis) and does not enforce
> `expires_at` — no wall clock without NTP. The issuer (Pi/laptop) enforces
> expiry. Add NTP if the ESP must self-enforce.

## Verified on hardware (2026-10-21)

Laptop issued commands over the LAN to the Pi broker; both devices returned the
full event chain.

| Device | IP | ping | blink_led | expiry |
|--------|-----|------|-----------|--------|
| Pi (`raspberrypi`) | YOUR_PI_IP | ✅ | stub | `received→expired` ✅ |
| ESP32-S3 (`esp32_s3_01`) | LAN_IP | ✅ | ran 1001 ms | issuer-enforced |

## Not wired yet

The laptop FastAPI `estop`/`capture` endpoints still return HTTP 501; they do
not publish to the broker yet. Commands above were sent with a standalone paho
publisher. Wiring the API to the broker is the next step.
