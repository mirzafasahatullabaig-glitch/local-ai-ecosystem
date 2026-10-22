"""3.5" ILI9486 SPI TFT status display for the eco ecosystem.

Renders a device-status panel with Pillow and pushes it to the TFT over raw
SPI, reusing the exact init sequence proven in test_tft.py (CS=8, DC=24,
RST=25, 18-bit colour, 320x480). Subscribes to the broker and redraws when a
device's manifest/state/event changes.

Deps (already on the Pi): spidev, RPi.GPIO, Pillow, paho-mqtt.
Color emoji on the panel needs: sudo apt install fonts-noto-color-emoji libraqm0
(without them, emoji are skipped and only the text labels show — no crash).
Run (needs GPIO -> sudo, with the venv python):
  sudo ECO_MQTT_USER=eco ECO_MQTT_PASS=... ~/tft-test-env/bin/python tft_status.py
  (or whichever venv has paho; falls back to no-broker demo if paho missing)
"""
from __future__ import annotations

import functools
import glob
import json
import os
import textwrap
import time
import sys

# systemd gives us a pipe, so prints were block-buffered and never reached
# the journal (eco-touch had logged 0 lines ever). Flush per line.
sys.stdout.reconfigure(line_buffering=True)

try:   # only for the pad-3 live camera preview; the status screen works without it
    import cv2
except ImportError:
    cv2 = None

import spidev
import RPi.GPIO as GPIO
from PIL import Image, ImageDraw, ImageFont

# --- display wiring (matches test_tft.py) ---
CS, DC, RST = 8, 24, 25
WIDTH, HEIGHT = 320, 480   # ILI9486 native portrait

GPIO.setwarnings(False)   # pins stay claimed across service restarts; harmless
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


def blit_region(img: Image.Image, x0: int = 0, y0: int = 0) -> None:
    """Push an RGB image to the panel at (x0, y0) (18-bit, component>>2).
    A part-screen push is what makes the live camera preview usable."""
    w, h = img.size
    command(0x2A); data([x0 >> 8, x0 & 0xFF, (x0 + w - 1) >> 8, (x0 + w - 1) & 0xFF])
    command(0x2B); data([y0 >> 8, y0 & 0xFF, (y0 + h - 1) >> 8, (y0 + h - 1) & 0xFF])
    command(0x2C)
    # Same bytes as the old per-pixel loop (R,G,B each >>2), built in C by Pillow.
    data(list(img.convert("RGB").point(lambda v: v >> 2).tobytes()))


def blit(img: Image.Image) -> None:
    """Push a full 320x480 frame."""
    blit_region(img)


# --- rendering ---
def load_font(size: int):
    try:
        return ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", size)
    except Exception:
        return ImageFont.load_default()


def load_emoji(size: int):
    # Color emoji needs its own font + libraqm; falls back to None if the
    # package isn't installed (apt install fonts-noto-color-emoji libraqm0).
    for path in ("/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
                 "/usr/share/fonts/truetype/noto-color-emoji/NotoColorEmoji.ttf"):
        try:
            return ImageFont.truetype(path, size, layout_engine=ImageFont.Layout.RAQM)
        except Exception:
            continue
    return None


FONT_TITLE = load_font(26)
FONT = load_font(20)
FONT_SM = load_font(16)
# NotoColorEmoji ships a single 109px bitmap strike; it only loads at that size
# (any other raises "invalid pixel size"). emoji_text downscales from here.
EMOJI = load_emoji(109)


@functools.lru_cache(maxsize=32)
def emoji_tile(emoji: str, size: int) -> Image.Image:
    """NotoColorEmoji is a fixed-strike bitmap font (only 109px), so draw at 109
    and downscale. Cached: the same few emoji are redrawn every frame."""
    tile = Image.new("RGBA", (109, 109), (0, 0, 0, 0))
    ImageDraw.Draw(tile).text((0, 0), emoji, font=EMOJI, embedded_color=True)
    return tile.resize((size, size), Image.LANCZOS)


def emoji_text(img, xy, emoji, text, font, fill):
    """Draw `emoji text` onto `img` at xy. Falls back to text-only if the
    emoji font didn't load."""
    x, y = xy
    size = font.size if hasattr(font, "size") else 20
    if EMOJI is not None and emoji:
        g = emoji_tile(emoji, size)
        img.paste(g, (x, y), g)   # alpha mask keeps the panel background
        x += int(size * 1.2)
    if text:
        ImageDraw.Draw(img).text((x, y), text, font=font, fill=fill)
    return x


FACE_H = 210   # top region for the bot face; status + MSG + temp fill the rest
MSG_FILE = "/tmp/eco_tft_msg.txt"
MSG_TTL_S = 90  # a message (and its talking/alert face) fades after this

# Per-mood accent colour + a short caption shown under the face.
MOODS = {
    "idle":     ((90, 200, 255),  "idle"),
    "speaking": ((120, 230, 160), "talking"),
    "alert":    ((255, 90, 70),   "ALERT"),
    "offline":  ((110, 120, 135), "offline"),
}


def human_age(sec: float) -> str:
    if sec < 2:   return "now"
    if sec < 60:  return f"{int(sec)}s"
    if sec < 3600: return f"{int(sec // 60)}m"
    return f"{int(sec // 3600)}h"


def draw_face(img, mood: str, blink: bool) -> None:
    """Draw the bot face in the top FACE_H px. Expression is driven by `mood`;
    eyes close when `blink`. All Pillow primitives — fast, no font needed."""
    d = ImageDraw.Draw(img)
    accent, caption = MOODS.get(mood, MOODS["idle"])
    cx, cy = WIDTH // 2, FACE_H // 2 - 10

    # head
    d.rounded_rectangle([40, 24, WIDTH - 40, FACE_H - 40], radius=36,
                        fill=(22, 30, 46), outline=accent, width=4)

    # eyes: open = filled circle with a glint; blink / offline = a flat line.
    eye_y, eye_dx, r = cy - 18, 60, 26
    for ex in (cx - eye_dx, cx + eye_dx):
        if blink or mood == "offline":
            d.line([ex - r, eye_y, ex + r, eye_y], fill=accent, width=6)
        else:
            d.ellipse([ex - r, eye_y - r, ex + r, eye_y + r], fill=accent)
            d.ellipse([ex + 4, eye_y - 10, ex + 14, eye_y], fill=(255, 255, 255))

    # mouth: smile when idle, flat when offline, 'O' on alert; when speaking it
    # alternates open/smile across redraws so the bot looks like it's talking.
    mx, my, mw = cx, cy + 46, 54
    if mood == "speaking" and int(time.time()) % 4 < 2:
        d.ellipse([mx - 30, my - 8, mx + 30, my + 18], fill=accent)
    elif mood == "alert":
        d.ellipse([mx - 22, my - 16, mx + 22, my + 20], outline=accent, width=6)
    elif mood == "offline":
        d.line([mx - mw, my, mx + mw, my], fill=accent, width=6)
    else:
        d.arc([mx - mw, my - 30, mx + mw, my + 24], start=20, end=160,
              fill=accent, width=6)

    # caption chip under the face
    bbox = d.textbbox((0, 0), caption, font=FONT_SM)
    tw = bbox[2] - bbox[0]
    d.text(((WIDTH - tw) // 2, FACE_H - 34), caption, font=FONT_SM, fill=accent)


def render(devices: dict, reading: dict) -> Image.Image:
    img = Image.new("RGB", (WIDTH, HEIGHT), (10, 12, 20))
    d = ImageDraw.Draw(img)

    # Latest agent message drives both the MSG band and the face mood. The file
    # is never cleared by the agent, so treat it as stale after MSG_TTL_S.
    try:
        if time.time() - os.path.getmtime(MSG_FILE) > MSG_TTL_S:
            msg = ""
        else:
            with open(MSG_FILE) as f:
                msg = f.read().strip()
    except OSError:
        msg = ""

    any_live = any(info.get("live") for info in devices.values())
    if not any_live and devices:
        mood = "offline"
    elif "HOT" in msg.upper():
        mood = "alert"
    elif msg:
        mood = "speaking"
    else:
        mood = "idle"
    blink = (int(time.time()) % 5 == 0)   # ~1 blink per 5s redraw cadence
    draw_face(img, mood, blink)

    # --- status region below the face ---
    y0 = FACE_H
    d.rectangle([0, y0, WIDTH, y0 + 34], fill=(30, 50, 90))
    live_n = sum(1 for i in devices.values() if i.get("live"))
    emoji_text(img, (10, y0 + 5), "🤖",
               f"eco · {live_n}/{len(devices)} live", FONT, (255, 255, 255))
    # Temp at a glance in the strip (full reading still in the bottom band).
    t = reading.get("temperature_c") if reading else None
    if t is not None:
        d.text((WIDTH - 78, y0 + 8), f"{t:.1f}°C", font=FONT, fill=(170, 225, 255))

    msg_bottom = HEIGHT - 72   # keep clear of the temp band (bottom 70px)
    y = y0 + 44
    for name, info in sorted(devices.items()):
        if y + 30 > msg_bottom:   # too many devices: stop before the temp band
            break
        live = info.get("live")
        emoji_text(img, (12, y), "🟢" if live else "🔴", name, FONT_SM, (235, 235, 235))
        age = info.get("age")
        label = ("live · " + human_age(age)) if (live and age is not None) else "offline"
        d.text((WIDTH - 90, y + 1), label, font=FONT_SM, fill=(150, 160, 175))
        y += 30

    if msg and y + 4 + 24 + 18 <= msg_bottom:   # room for the label + >=1 line
        band_top = y + 4
        # Only as many lines as physically fit above the temp band — never draw
        # into it. The label eats the first 24px, each line 18px.
        fit = (msg_bottom - band_top - 24) // 18
        wrapped = textwrap.wrap(msg, width=26)
        lines = wrapped[:fit]
        if len(wrapped) > fit:   # truncated -> mark it
            lines[-1] = lines[-1][:23] + "..."
        band_bot = band_top + 24 + len(lines) * 18
        d.rectangle([0, band_top, WIDTH, band_bot], fill=(70, 40, 20))
        emoji_text(img, (12, band_top + 4), "💬", "MSG:", FONT_SM, (255, 200, 120))
        for i, line in enumerate(lines):
            d.text((12, band_top + 24 + i * 18), line, font=FONT_SM, fill=(255, 230, 190))

    if reading:
        d.rectangle([0, HEIGHT - 70, WIDTH, HEIGHT], fill=(20, 30, 45))
        t = reading.get("temperature_c")
        h = reading.get("humidity")
        if t is not None:
            hum = f"  {h:.0f}% RH" if h is not None else ""   # DHT may omit it
            emoji_text(img, (12, HEIGHT - 60), "🌡", f"{t:.1f}°C{hum}",
                       FONT_TITLE, (120, 220, 255))
    d.text((WIDTH - 90, HEIGHT - 24), time.strftime("%H:%M:%S"), font=FONT_SM,
           fill=(120, 130, 145))
    return img


# --- pad 3: live camera preview, snapshot on release ---
# The touch service (also root) creates PREVIEW_FLAG while pad 3 is held and
# removes it on release; we stream the webcam meanwhile, then save the last
# frame to SNAP_FILE for it to send to the station.
PREVIEW_FLAG = "/tmp/eco_cam_preview"
SNAP_FILE = "/tmp/eco_cam_snap.jpg"
PREVIEW_Y = 120   # 320x240 video centred in the 480-tall panel


def camera_path() -> str | None:
    """Same rule as pi_agent: /dev/v4l/by-id lists only real USB cameras."""
    found = sorted(glob.glob("/dev/v4l/by-id/*-video-index0"))
    return os.environ.get("ECO_CAMERA_DEV") or (found[0] if found else None)


def preview_frame_screen(title: str, hint: str) -> Image.Image:
    img = Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0))
    d = ImageDraw.Draw(img)
    for y, text, font in ((40, title, FONT_TITLE), (HEIGHT - 90, hint, FONT)):
        tw = d.textbbox((0, 0), text, font=font)[2]
        d.text(((WIDTH - tw) // 2, y), text, font=font, fill=(255, 255, 255))
    return img


def run_preview() -> None:
    """Stream the webcam while PREVIEW_FLAG exists, then write the freshest
    frame to SNAP_FILE and free the camera (the agent's capture needs it)."""
    path = camera_path()
    if cv2 is None or not path:
        blit(preview_frame_screen("no camera", "plug in a USB webcam"))
        while os.path.exists(PREVIEW_FLAG):
            time.sleep(0.05)
        return
    blit(preview_frame_screen("camera", "release to snap"))
    cap = cv2.VideoCapture(path, cv2.CAP_V4L2)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
    frame = None
    shown, started = 0, time.time()
    try:
        while os.path.exists(PREVIEW_FLAG):
            ok, f = cap.read()
            if not ok:
                time.sleep(0.05)
                continue
            small = cv2.cvtColor(cv2.resize(f, (WIDTH, 240)), cv2.COLOR_BGR2RGB)
            blit_region(Image.fromarray(small), 0, PREVIEW_Y)
            shown += 1
        # The driver queues a few frames; drop them so the snap is the moment
        # the finger lifted, not ~4 frames before.
        for _ in range(4):
            cap.grab()
        ok, f = cap.read()
        frame = f if ok else None
    finally:
        cap.release()
    if frame is not None:
        tmp = SNAP_FILE + ".tmp.jpg"
        cv2.imwrite(tmp, frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
        os.replace(tmp, SNAP_FILE)   # atomic: the touch service never reads half a file
    held = time.time() - started
    print(f"preview: {shown} frames in {held:.1f}s ({shown / max(held, 0.01):.1f} fps), "
          f"snap {'saved' if frame is not None else 'FAILED'}")
    blit(preview_frame_screen("snap!", "looking at it..."))


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
            if os.path.exists(PREVIEW_FLAG):
                run_preview()
            now = time.time()
            for name in list(devices):
                age = now - last_seen.get(name, 0)
                devices[name]["live"] = age * 1000 < STALE_MS
                devices[name]["age"] = age   # seconds since last message, for the UI
            blit(render(devices, reading))
            # full redraw ~ every 2s (SPI blit is the slow part), but react to
            # a pad-3 press within 0.1s
            for _ in range(20):
                if os.path.exists(PREVIEW_FLAG):
                    break
                time.sleep(0.1)
    except KeyboardInterrupt:
        pass
    finally:
        client.loop_stop(); client.disconnect()
        spi.close(); GPIO.cleanup()


if __name__ == "__main__":
    main()
