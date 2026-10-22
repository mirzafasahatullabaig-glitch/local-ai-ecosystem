"""Pi-side device agent for the local-ai-ecosystem (Phase 2).

Connects to the MQTT broker, announces its Manifest, listens on its cmd topic,
and walks the Phase-1 state machine emitting Events. It reuses schemas/messages
(copied here as messages.py) verbatim — no protocol is redefined.

Run:  ~/eco-agent/.venv/bin/python ~/eco-agent/pi_agent.py
"""
from __future__ import annotations

import base64
import collections
import glob
import json
import os
import socket
import subprocess
import tempfile
import sys
import time
import uuid

import paho.mqtt.client as mqtt

import messages as proto  # the copied schemas/messages.py

DEVICE_ID = os.environ.get("ECO_DEVICE_ID", "raspberrypi")
BROKER = os.environ.get("ECO_BROKER", "127.0.0.1")
PORT = int(os.environ.get("ECO_BROKER_PORT", "1883"))

CMD_TOPIC = proto.device_topic(DEVICE_ID, "cmd")
EVT_TOPIC = proto.device_topic(DEVICE_ID, "evt")
MANIFEST_TOPIC = proto.device_topic(DEVICE_ID, "manifest")
STATE_TOPIC = proto.device_topic(DEVICE_ID, "state")


def camera_device() -> str | None:
    """A USB webcam, if one is plugged in. /dev/v4l/by-id lists only real
    cameras (the Pi's /dev/video10-31 are its codec/ISP, not cameras).
    ECO_CAMERA_DEV overrides. Checked at connect: plug in, then restart."""
    override = os.environ.get("ECO_CAMERA_DEV")
    if override:
        return override
    found = sorted(glob.glob("/dev/v4l/by-id/*-video-index0"))
    return found[0] if found else None


def build_manifest() -> proto.Manifest:
    """What this Pi can do. Capabilities are declared, not hardcoded elsewhere."""
    caps = [
        proto.Capability(name="ping", max_duration_ms=1000, physical=False, risk="none"),
        proto.Capability(name="blink_led", max_duration_ms=5000, physical=True, risk="low"),
        proto.Capability(name="tft_show", max_duration_ms=1000, physical=True, risk="none"),
        proto.Capability(name="update", max_duration_ms=60000, physical=False, risk="high"),
    ]
    if camera_device():   # only offer what the hardware can actually do
        caps.append(proto.Capability(name="capture_image", max_duration_ms=10000,
                                     physical=True, risk="low"))
    return proto.Manifest(
        msg_id=uuid.uuid4().hex,
        device_id=DEVICE_ID,
        device_type="raspberry_pi",
        hw=f"{os.uname().machine} {socket.gethostname()}",
        fw_version="0.3",
        protocol_version=1,
        capabilities=caps,
    )


# Where tft_show writes its message; the TFT display script reads this file.
TFT_MSG_FILE = "/tmp/eco_tft_msg.txt"


def emit_event(client: mqtt.Client, command_id: str, state: proto.State,
               extra: dict | None = None) -> None:
    evt = proto.Event(
        msg_id=uuid.uuid4().hex,
        device_id=DEVICE_ID,
        command_id=command_id,
        state=state,
    )
    # extra rides alongside the Event fields (e.g. image_b64), like the ESP's
    # read_dht result; receivers that only parse Event ignore it.
    body = evt.model_dump(mode="json") | (extra or {})
    client.publish(EVT_TOPIC, json.dumps(body), qos=1)
    print(f"  -> evt {state.value}")


_seen_ids: collections.deque[str] = collections.deque(maxlen=64)


def handle_command(client: mqtt.Client, payload: bytes) -> None:
    # Parse through the schema — never hand-roll. A bad command is rejected, not crashed on.
    try:
        cmd = proto.Command.model_validate_json(payload)
    except Exception as e:  # noqa: BLE001 - want any validation failure here
        print(f"  !! bad command rejected: {e}")
        return

    # QoS 1 means "at least once": after a reconnect the broker may redeliver,
    # and a retry may resend. Run each command_id once (recovery test: both
    # devices ran a duplicate twice). 64 ids >> commands in flight at a time.
    if cmd.command_id in _seen_ids:
        print(f"command {cmd.command_id}: duplicate ignored")
        return
    _seen_ids.append(cmd.command_id)

    print(f"command {cmd.command_id}: action={cmd.action} seq={cmd.seq}")
    emit_event(client, cmd.command_id, proto.State.RECEIVED)

    if cmd.is_expired():
        emit_event(client, cmd.command_id, proto.State.EXPIRED)
        return

    emit_event(client, cmd.command_id, proto.State.ACCEPTED)
    emit_event(client, cmd.command_id, proto.State.EXECUTING)

    # The actual "work". Phase 2 keeps this symbolic: ping is instant, blink
    # sleeps briefly. No LLM-generated code runs here.
    extra = None
    if cmd.action == "capture_image":
        ok, extra = _capture_image()
    else:
        ok = _do_action(cmd.action, cmd.params)

    emit_event(client, cmd.command_id,
               proto.State.COMPLETED if ok else proto.State.FAILED, extra)


def _do_action(action: str, params: dict) -> bool:
    if action == "ping":
        return True
    if action == "blink_led":
        # shortcut: no real GPIO yet, just a timed stub. Upgrade when the LED is wired.
        time.sleep(min(float(params.get("ms", 500)) / 1000.0, 5.0))
        return True
    if action == "tft_show":
        # Write a message the TFT display script picks up and renders.
        msg = str(params.get("text", ""))[:300]  # fits ~6 wrapped lines on the TFT
        try:
            with open(TFT_MSG_FILE, "w") as f:
                f.write(msg)
            return True
        except OSError as e:
            print(f"  !! tft_show write failed: {e}")
            return False
    if action == "update":
        return _self_update()
    print(f"  !! unknown action {action!r}")
    return False


def _capture_image() -> tuple[bool, dict | None]:
    """One 640x480 JPEG from the USB webcam, base64 in the result event
    (~60-100 KB on MQTT, fine for Mosquitto). -S skips the first frames so
    auto-exposure settles: on the Jieli webcam 10 frames still gave black or
    dark shots (mean brightness 0-54/255), 30 settled (189/255), 60 no better."""
    dev = camera_device()
    if not dev:
        print("  !! capture_image: no camera")
        return False, None
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
        path = f.name
    try:
        out = subprocess.run(["fswebcam", "-q", "-d", dev, "-r", "640x480", "-S", "30",
                              "--no-banner", "--jpeg", "85", path],
                             capture_output=True, text=True, timeout=10)
        if out.returncode != 0 or os.path.getsize(path) == 0:
            print(f"  !! capture_image failed: {out.stderr.strip()[:200]}")
            return False, None
        with open(path, "rb") as f:
            return True, {"image_b64": base64.b64encode(f.read()).decode()}
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"  !! capture_image failed: {e}")
        return False, None
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _self_update() -> bool:
    """Pull the latest agent from git and restart. High-risk: only reachable by
    an authenticated client on the broker. shortcut: no signature check, trusts
    the broker's password auth; add signing before exposing beyond the LAN.
    """
    repo = os.path.dirname(os.path.abspath(__file__))
    try:
        out = subprocess.run(["git", "-C", repo, "pull", "--ff-only"],
                             capture_output=True, text=True, timeout=30)
        print(f"  update: {out.stdout.strip()} {out.stderr.strip()}")
        if out.returncode != 0:
            return False
        # Re-exec this process with the updated code.
        print("  update ok, restarting agent")
        os.execv(sys.executable, [sys.executable] + sys.argv)
    except Exception as e:  # noqa: BLE001
        print(f"  !! update failed: {e}")
        return False
    return True  # not reached (execv replaces the process)


def on_connect(client, userdata, flags, reason_code, properties=None):
    if reason_code != 0:
        print(f"connect failed: {reason_code}")
        return
    print(f"connected to {BROKER}:{PORT} as {DEVICE_ID}")
    client.subscribe(CMD_TOPIC, qos=1)
    print(f"subscribed {CMD_TOPIC}")
    client.publish(MANIFEST_TOPIC, build_manifest().model_dump_json(), qos=1, retain=True)
    print(f"published manifest -> {MANIFEST_TOPIC}")
    client.publish(STATE_TOPIC, json.dumps({"online": True, "ts": proto.now_ms()}),
                   qos=1, retain=True)


def on_message(client, userdata, msg):
    handle_command(client, msg.payload)


def main() -> None:
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"agent-{DEVICE_ID}")
    user, pw = os.environ.get("ECO_MQTT_USER"), os.environ.get("ECO_MQTT_PASS")
    if user:
        client.username_pw_set(user, pw)
    # Last will: if the agent drops, the broker marks us offline on the state topic.
    client.will_set(STATE_TOPIC, json.dumps({"online": False}), qos=1, retain=True)
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(BROKER, PORT, keepalive=30)
    client.loop_start()
    print("agent running; Ctrl-C to stop")
    try:
        while True:
            # Periodic heartbeat so the API keeps us live (stale after 15s idle).
            client.publish(STATE_TOPIC, json.dumps({"online": True, "ts": proto.now_ms()}),
                           qos=0, retain=True)
            time.sleep(5)
    except KeyboardInterrupt:
        client.publish(STATE_TOPIC, json.dumps({"online": False}), qos=1, retain=True)
        client.loop_stop()
        client.disconnect()
        print("\nstopped")
        sys.exit(0)


if __name__ == "__main__":
    main()
