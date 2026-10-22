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

# pad (BCM) -> commands it sends. Pad 1 beeps and blinks the ESP together;
# pad 2 reads the DHT (the result lands on the ESP OLED, the Pi TFT and the
# station). Pad 3 is vision: the station photographs through the Pi webcam
# and describes it on the TFT. Pad 4 is push-to-talk. Pads 3-4 go to the
# laptop over HTTP, not MQTT -- see voice.py.
TOUCH_PINS = {
    5:  [(ESP, "beep", {"ms": 300}), (ESP, "blink_led", {"ms": 1000})],
    6:  [(ESP, "read_dht", {})],
}
LOOK_PIN = 13   # pad 3 -> voice.look_once() (Pi camera -> station vision -> TFT)
VOICE_PIN = 16  # pad 4 -> voice.listen_once() (record + upload to the station)


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
    pins = list(TOUCH_PINS) + [LOOK_PIN, VOICE_PIN]
    for pin in pins:
        GPIO.setup(pin, GPIO.IN)

    def tft(text: str) -> None:
        # Through the agent's tft_show, not by writing the message file: this
        # service runs as root, and a root-owned file would lock the agent out.
        client.publish(f"eco/v1/devices/{PI}/cmd", build_cmd(PI, "tft_show", {"text": text}), qos=1)

    # Simple edge poll with debounce: TTP224 holds HIGH while touched. Fire once
    # per touch (rising edge), not continuously.
    last = {pin: False for pin in pins}
    try:
        while True:
            for pin, cmds in TOUCH_PINS.items():
                now = GPIO.input(pin) == GPIO.HIGH
                if now and not last[pin]:
                    for target, action, params in cmds:
                        cmd = build_cmd(target, action, params)
                        client.publish(f"eco/v1/devices/{target}/cmd", cmd, qos=1)
                        print(f"pad GPIO{pin} -> {target}/{action}")
                last[pin] = now
            # Pad 3: look. Blocks while the station photographs + describes
            # (~2s warm, ~15s on the first vision call); fine for a tap.
            look_now = GPIO.input(LOOK_PIN) == GPIO.HIGH
            if look_now and not last[LOOK_PIN]:
                print(f"pad GPIO{LOOK_PIN} -> look")
                tft("looking...")
                import voice
                problem = voice.look_once()
                if problem:
                    tft(problem)
            last[LOOK_PIN] = look_now
            # Pad 4: push-to-talk. Record + upload locally (blocks ~4s, fine).
            voice_now = GPIO.input(VOICE_PIN) == GPIO.HIGH
            if voice_now and not last[VOICE_PIN]:
                print(f"pad GPIO{VOICE_PIN} -> voice")
                import voice
                voice.listen_once()
            last[VOICE_PIN] = voice_now
            time.sleep(0.03)   # 30ms poll, enough debounce for a finger tap
    except KeyboardInterrupt:
        pass
    finally:
        GPIO.cleanup()
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    main()
