"""Push-to-talk voice capture for the Pi (INMP441 I2S mic -> laptop STT).

Records a short clip with `arecord` (ALSA, no python audio deps) and POSTs it
to the laptop's /listen endpoint, which transcribes on the GPU and runs the
text through /ask. The LLM answer comes back on the Pi TFT via the existing
tft_show mirror, so this script only has to capture + upload.

Triggered by touch pad 4 (see touch_ttp224.py). Can also run standalone to test:
  ECO_STATION=http://LAN_IP:8000 ECO_API_KEY=... python3 voice.py

INMP441 wiring to the Pi I2S (BCM):
  VDD -> 3V3 | GND -> GND | L/R -> GND (left channel)
  WS (LRCL) -> GPIO19 (pin 35) | SCK (BCLK) -> GPIO18 (pin 12)
  SD (DOUT)  -> GPIO20 (pin 38)
Enable I2S first: add `dtparam=i2s=on` and `dtoverlay=googlevoicehat-soundcard`
(or `dtoverlay=i2s-mmap`) to /boot/firmware/config.txt, reboot, then
`arecord -l` should list the I2S capture device. Set ECO_ALSA_DEV to it.
"""
from __future__ import annotations

import os
import subprocess
import tempfile

import urllib.request

STATION = os.environ.get("ECO_STATION", "http://LAN_IP:8000")  # laptop
API_KEY = os.environ.get("ECO_API_KEY", "")
ALSA_DEV = os.environ.get("ECO_ALSA_DEV", "plughw:1,0")  # I2S card from `arecord -l`
SECONDS = int(os.environ.get("ECO_VOICE_SECONDS", "4"))


def record(path: str) -> bool:
    """Capture SECONDS of 16kHz mono 16-bit PCM from the I2S mic into a WAV."""
    try:
        subprocess.run(
            ["arecord", "-D", ALSA_DEV, "-f", "S16_LE", "-r", "16000",
             "-c", "1", "-d", str(SECONDS), path],
            check=True, capture_output=True, timeout=SECONDS + 5,
        )
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        err = getattr(e, "stderr", b"") or b""
        print(f"  !! record failed: {err.decode(errors='replace')[:200]}")
        return False


def upload(path: str) -> str:
    """POST the WAV to the laptop /listen as multipart; return the transcript."""
    with open(path, "rb") as f:
        audio = f.read()
    boundary = "----ecoVoiceBoundary"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="audio"; filename="clip.wav"\r\n'
        "Content-Type: audio/wav\r\n\r\n"
    ).encode() + audio + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(f"{STATION}/listen", data=body, method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    if API_KEY:
        req.add_header("X-API-Key", API_KEY)
    with urllib.request.urlopen(req, timeout=60) as r:
        import json
        return json.loads(r.read()).get("transcript", "")


LOOK_PROMPT = "What do you see? Answer in one short sentence."


def look_once() -> str:
    """Pad 3: ask the station to look through the Pi's USB webcam. The station
    takes the photo via the agent's capture_image (the same camera path the
    panel's "Pi camera" button uses) and mirrors the answer to the TFT.
    Returns a short status for the TFT if something went wrong, else ""."""
    import json
    import urllib.error
    req = urllib.request.Request(
        f"{STATION}/ask", method="POST",
        data=json.dumps({"text": LOOK_PROMPT, "camera": True}).encode(),
        headers={"Content-Type": "application/json"})
    if API_KEY:
        req.add_header("X-API-Key", API_KEY)
    try:
        # long timeout: photo (~1s) + vision model load on first use (~12s)
        with urllib.request.urlopen(req, timeout=120) as r:
            print(f"saw: {json.loads(r.read()).get('answer')!r}  (answer is on the TFT)")
            return ""
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read()).get("detail", e.reason)
        except ValueError:
            detail = e.reason
        print(f"  !! look failed: {e.code} {detail}")
        return f"camera: {detail}"
    except Exception as e:  # noqa: BLE001 - one failed look must not kill the loop
        print(f"  !! look failed: {e}")
        return "vision failed - is the station up?"


def listen_once() -> None:
    """One push-to-talk cycle: record, upload, print what was heard."""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        path = f.name
    try:
        print(f"recording {SECONDS}s from {ALSA_DEV}...")
        if not record(path):
            return
        print("uploading to station...")
        transcript = upload(path)
        print(f"heard: {transcript!r}  (answer is on the TFT)")
    except Exception as e:  # noqa: BLE001 - one failed turn must not kill the loop
        print(f"  !! voice turn failed: {e}")
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


if __name__ == "__main__":
    listen_once()
