"""3.5" ILI9486 SPI TFT status display for the eco ecosystem.

Renders a device-status panel with Pillow and pushes it to the TFT over raw
SPI, reusing the exact init sequence proven in test_tft.py (CS=8, DC=24,
RST=25, 18-bit colour, 320x480). Subscribes to the broker and redraws when a
device's manifest/state/event changes.

Deps (already on the Pi): spidev, RPi.GPIO, Pillow, paho-mqtt.
Run (needs GPIO -> sudo, with the venv python):
  sudo ECO_MQTT_USER=eco ECO_MQTT_PASS=... ~/tft-test-env/bin/python tft_status.py
  (or whichever venv has paho; falls back to no-broker demo if paho missing)
"""
from __future__ import annotations

import json
import os
import time

import spidev
import RPi.GPIO as GPIO
from PIL import Image, ImageDraw, ImageFont

# --- display wiring (matches test_tft.py) ---
CS, DC, RST = 8, 24, 25
WIDTH, HEIGHT = 320, 480   # ILI9486 native portrait

GPIO.setmode(GPIO.BCM)
GPIO.setup(DC, GPIO.OUT)
GPIO.setup(RST, GPIO.OUT)
spi = spidev.SpiDev()
spi.open(0, 0)
spi.max_speed_hz = 16_000_000   # faster than the 4MHz test; ILI9486 tolerates it
spi.mode = 0


def command(value: int) -> None:
    GPIO.output(DC, 0)
    spi.xfer2([value])


def data(values: list[int]) -> None:
    GPIO.output(DC, 1)
    for i in range(0, len(values), 4096):
        spi.xfer2(values[i:i + 4096])


def init_display() -> None:
    GPIO.output(RST, 1); time.sleep(0.05)
    GPIO.output(RST, 0); time.sleep(0.1)
    GPIO.output(RST, 1); time.sleep(0.15)
    command(0x01); time.sleep(0.15)   # software reset
    command(0x11); time.sleep(0.12)   # sleep out
    command(0x3A); data([0x66])       # 18-bit RGB
    command(0x36); data([0x48])       # memory access (same orientation as test)
    command(0x13)                     # normal display
    command(0x29); time.sleep(0.1)    # display on


def blit(img: Image.Image) -> None:
    """Push a full 320x480 RGB image to the panel (18-bit, component>>2)."""
    command(0x2A); data([0, 0, (WIDTH - 1) >> 8, (WIDTH - 1) & 0xFF])
    command(0x2B); data([0, 0, (HEIGHT - 1) >> 8, (HEIGHT - 1) & 0xFF])
    command(0x2C)
    px = img.load()
    buf = []
    for y in range(HEIGHT):
        for x in range(WIDTH):
            r, g, b = px[x, y][:3]
            buf += [r >> 2, g >> 2, b >> 2]
    data(buf)


# --- rendering ---
def load_font(size: int):
    try:
        return ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", size)
    except Exception:
        return ImageFont.load_default()


FONT_TITLE = load_font(26)
FONT = load_font(20)
FONT_SM = load_font(16)


def render(devices: dict, reading: dict) -> Image.Image:
    img = Image.new("RGB", (WIDTH, HEIGHT), (10, 12, 20))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, WIDTH, 44], fill=(30, 50, 90))
    d.text((10, 8), "eco · devices", font=FONT_TITLE, fill=(255, 255, 255))

    y = 60
    for name, info in sorted(devices.items()):
        live = info.get("live")
        dot = (60, 210, 140) if live else (220, 70, 70)
        d.ellipse([12, y + 6, 26, y + 20], fill=dot)
        d.text((38, y), name, font=FONT, fill=(235, 235, 235))
        d.text((38, y + 24), "live" if live else "offline",
               font=FONT_SM, fill=(150, 160, 175))
        y += 58

    # A tft_show message from the agent, if any (shown above the reading band).
    try:
        with open("/tmp/eco_tft_msg.txt") as f:
            msg = f.read().strip()
    except OSError:
        msg = ""
    if msg:
        # Wrap to the panel width so a full LLM answer is readable, not clipped
        # at 22 chars. ~26 chars/line at FONT_SM, up to 6 lines then ellipsis.
        import textwrap
        lines = textwrap.wrap(msg, width=26)[:6]
        if len(textwrap.wrap(msg, width=26)) > 6:
            lines[-1] = lines[-1][:23] + "..."
        band_top = HEIGHT - 70 - (28 + len(lines) * 18)
        d.rectangle([0, band_top, WIDTH, HEIGHT - 70], fill=(70, 40, 20))
        d.text((12, band_top + 6), "MSG:", font=FONT_SM, fill=(255, 200, 120))
        for i, line in enumerate(lines):
            d.text((12, band_top + 26 + i * 18), line, font=FONT_SM, fill=(255, 230, 190))

    if reading:
        d.rectangle([0, HEIGHT - 70, WIDTH, HEIGHT], fill=(20, 30, 45))
        t = reading.get("temperature_c")
        h = reading.get("humidity")
        if t is not None:
            d.text((12, HEIGHT - 60), f"{t:.1f} C   {h:.0f}% RH",
                   font=FONT_TITLE, fill=(120, 220, 255))
    d.text((12, HEIGHT - 24), time.strftime("%H:%M:%S"), font=FONT_SM,
           fill=(120, 130, 145))
    return img


# --- broker ---
STALE_MS = 15_000


def main() -> None:
    init_display()
    blit(render({}, {}))   # boot screen

    devices: dict[str, dict] = {}
    reading: dict = {}
    last_seen: dict[str, float] = {}

    try:
        import paho.mqtt.client as mqtt
    except Exception:
        print("paho not in this python; showing static boot screen only")
        time.sleep(5); return

    def mark(name):
        last_seen[name] = time.time()

    def on_connect(c, u, f, rc, props=None):
        c.subscribe("eco/v1/devices/+/manifest", qos=1)
        c.subscribe("eco/v1/devices/+/state", qos=1)
        c.subscribe("eco/v1/devices/+/evt", qos=1)

    def on_message(c, u, msg):
        parts = msg.topic.split("/")
        name, kind = parts[3], parts[4]
        if kind == "manifest":
            devices.setdefault(name, {}); mark(name)
        elif kind == "state":
            try:
                st = json.loads(msg.payload)
            except Exception:
                return
            if st.get("online"):
                mark(name); devices.setdefault(name, {})
        elif kind == "evt":
            try:
                ev = json.loads(msg.payload)
            except Exception:
                return
            if "temperature_c" in ev:   # a read_dht result rides on evt
                reading.update({"temperature_c": ev["temperature_c"],
                                "humidity": ev.get("humidity")})

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="tft-status")
    user, pw = os.environ.get("ECO_MQTT_USER"), os.environ.get("ECO_MQTT_PASS")
    if user:
        client.username_pw_set(user, pw)
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(os.environ.get("ECO_BROKER", "127.0.0.1"), 1883, 30)
    client.loop_start()

    try:
        while True:
            now = time.time()
            for name in list(devices):
                devices[name]["live"] = (now - last_seen.get(name, 0)) * 1000 < STALE_MS
            blit(render(devices, reading))
            time.sleep(2)   # full redraw ~ every 2s (SPI blit is the slow part)
    except KeyboardInterrupt:
        pass
    finally:
        client.loop_stop(); client.disconnect()
        spi.close(); GPIO.cleanup()


if __name__ == "__main__":
    main()
