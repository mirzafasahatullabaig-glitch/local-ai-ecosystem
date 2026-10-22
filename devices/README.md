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

| Device | ping | blink_led | expiry |
|--------|------|-----------|--------|
| Pi (`raspberrypi`) | ✅ | stub | `received→expired` ✅ |
| **ESP32 classic (`esp32_dev_01`)** | ✅ | ✅ **blue LED on GPIO 2 confirmed blinking (2026-10-22)** | issuer-enforced |
| ESP32-S3 (`esp32_s3_01`) | ✅ | code ran, LED pin unconfirmed | issuer-enforced |

> blink_led runs correctly in firmware (executing→completed gap matches the
> requested ms) but the physical LED did not light — GPIO 48 is likely the
> wrong pin/LED type for this board. TODO: confirm board model; many S3 boards
> use an addressable WS2812 needing `rgbLedWrite(pin,r,g,b)`, XIAO S3 uses
> GPIO 21 active-LOW. Protocol path is fully verified regardless.

## API → broker (wired, verified 2026-10-21)

`backend/messaging/bus.py` publishes Commands and collects Events. The API's
`/ask` deterministic device actions (`estop`, `capture_image`, `ping`) pick
devices by capability and dispatch through it. The LLM never emits commands.

End-to-end verified: one call `/ask {"text":"ping all devices"}` published a
Command to both the Pi and the ESP32-S3 over MQTT and collected
`received→accepted→executing→completed` from each.

> shortcut: agents don't send periodic heartbeats, so a device goes stale 15s
> after register (`find_by_capability` then skips it). Re-register to refresh.
> Fix = a heartbeat endpoint + agent-side periodic pings.
