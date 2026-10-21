"""Pi-side device agent for the local-ai-ecosystem (Phase 2).

Connects to the MQTT broker, announces its Manifest, listens on its cmd topic,
and walks the Phase-1 state machine emitting Events. It reuses schemas/messages
(copied here as messages.py) verbatim — no protocol is redefined.

Run:  ~/eco-agent/.venv/bin/python ~/eco-agent/pi_agent.py
"""
from __future__ import annotations

import json
import os
import socket
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


def build_manifest() -> proto.Manifest:
    """What this Pi can do. Capabilities are declared, not hardcoded elsewhere."""
    return proto.Manifest(
        msg_id=uuid.uuid4().hex,
        device_id=DEVICE_ID,
        device_type="raspberry_pi",
        hw=f"{os.uname().machine} {socket.gethostname()}",
        fw_version="0.1",
        protocol_version=1,
        capabilities=[
            proto.Capability(name="ping", max_duration_ms=1000, physical=False, risk="none"),
            proto.Capability(name="blink_led", max_duration_ms=5000, physical=True, risk="low"),
        ],
    )


def emit_event(client: mqtt.Client, command_id: str, state: proto.State) -> None:
    evt = proto.Event(
        msg_id=uuid.uuid4().hex,
        device_id=DEVICE_ID,
        command_id=command_id,
        state=state,
    )
    client.publish(EVT_TOPIC, evt.model_dump_json(), qos=1)
    print(f"  -> evt {state.value}")


def handle_command(client: mqtt.Client, payload: bytes) -> None:
    # Parse through the schema — never hand-roll. A bad command is rejected, not crashed on.
    try:
        cmd = proto.Command.model_validate_json(payload)
    except Exception as e:  # noqa: BLE001 - want any validation failure here
        print(f"  !! bad command rejected: {e}")
        return

    print(f"command {cmd.command_id}: action={cmd.action} seq={cmd.seq}")
    emit_event(client, cmd.command_id, proto.State.RECEIVED)

    if cmd.is_expired():
        emit_event(client, cmd.command_id, proto.State.EXPIRED)
        return

    emit_event(client, cmd.command_id, proto.State.ACCEPTED)
    emit_event(client, cmd.command_id, proto.State.EXECUTING)

    # The actual "work". Phase 2 keeps this symbolic: ping is instant, blink
    # sleeps briefly. No LLM-generated code runs here.
    ok = _do_action(cmd.action, cmd.params)

    emit_event(client, cmd.command_id,
               proto.State.COMPLETED if ok else proto.State.FAILED)


def _do_action(action: str, params: dict) -> bool:
    if action == "ping":
        return True
    if action == "blink_led":
        # shortcut: no real GPIO yet, just a timed stub. Upgrade when the LED is wired.
        time.sleep(min(float(params.get("ms", 500)) / 1000.0, 5.0))
        return True
    print(f"  !! unknown action {action!r}")
    return False


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
