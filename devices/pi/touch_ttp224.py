"""TTP224 4-pad capacitive touch -> MQTT commands.

Each pad, when touched, publishes a Command to a target device (by default the
ESP32) so a physical tap triggers a real action. Runs on the Pi alongside the
agent. Pure RPi.GPIO + paho, no extra display deps.

Wiring (BCM numbering — chosen to avoid the 3.5" SPI TFT, which uses the
lower GPIOs / SPI0; edit TOUCH_PINS to match your wiring):
  TTP224 OUT1..OUT4 -> GPIO 5, 6, 13, 16  (Pi physical pins 29, 31, 33, 36)
  VCC -> 3V3 (pin 17), GND -> GND (pin 20).
  TTP224 outputs HIGH on touch (active-high, default config).

Run: ECO_MQTT_USER=eco ECO_MQTT_PASS=... python3 touch_ttp224.py
"""
from __future__ import annotations

import os
import time
import uuid

import RPi.GPIO as GPIO
import paho.mqtt.client as mqtt

BROKER = os.environ.get("ECO_BROKER", "127.0.0.1")
PORT = int(os.environ.get("ECO_BROKER_PORT", "1883"))
USER = os.environ.get("ECO_MQTT_USER")
PASS = os.environ.get("ECO_MQTT_PASS")
ESP = "esp32_dev_01"
PI = "raspberrypi"

# pad (BCM) -> (target_device, action, params). Pads drive BOTH devices: pad 1-3
# command the ESP, pad 4 shows a message on the Pi's own TFT (touch -> Pi).
TOUCH_PINS = {
    5:  (ESP, "beep", {"ms": 300}),
    6:  (ESP, "blink_led", {"ms": 1000}),
    13: (ESP, "read_dht", {}),
    16: (PI,  "tft_show", {"text": "touched pad 4"}),
}


def build_cmd(target: str, action: str, params: dict) -> str:
    now = int(time.time() * 1000)
    return (
        '{{"schema_version":1,"msg_id":"{mid}","device_id":"{dev}",'
        '"command_id":"{cid}","action":"{act}","params":{prm},'
        '"issued_at":{iss},"expires_at":{exp},"seq":0,'
        '"origin":{{"user":"touch"}}}}'
    ).format(
        mid=uuid.uuid4().hex, dev=target, cid=uuid.uuid4().hex, act=action,
        prm=_json(params), iss=now, exp=now + 30000,
    )


def _json(params: dict) -> str:
    import json
    return json.dumps(params)  # params is our own small dict, safe to serialize


def main() -> None:
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="touch-ttp224")
    if USER:
        client.username_pw_set(USER, PASS)
    client.connect(BROKER, PORT, keepalive=30)
    client.loop_start()
    print(f"touch pads active via {BROKER}:{PORT}")

    GPIO.setmode(GPIO.BCM)
    for pin in TOUCH_PINS:
        GPIO.setup(pin, GPIO.IN)

    # Simple edge poll with debounce: TTP224 holds HIGH while touched. Fire once
    # per touch (rising edge), not continuously.
    last = {pin: False for pin in TOUCH_PINS}
    try:
        while True:
            for pin, (target, action, params) in TOUCH_PINS.items():
                now = GPIO.input(pin) == GPIO.HIGH
                if now and not last[pin]:
                    cmd = build_cmd(target, action, params)
                    client.publish(f"eco/v1/devices/{target}/cmd", cmd, qos=1)
                    print(f"pad GPIO{pin} -> {target}/{action}")
                last[pin] = now
            time.sleep(0.03)   # 30ms poll, enough debounce for a finger tap
    except KeyboardInterrupt:
        pass
    finally:
        GPIO.cleanup()
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    main()
