"""Publishes Commands to devices over MQTT and collects their Events.

A DeviceBus Protocol lets the API depend on an interface, so tests inject a
fake and pytest never touches a real broker — same pattern as the model
gateway. The MQTT implementation is only wired in the uvicorn factory.

The LLM never produces Commands. The API builds them from whitelisted
deterministic actions and sends them here.
"""
from __future__ import annotations

import threading
import uuid
from typing import Optional, Protocol

from schemas.messages import Command, Event, Origin, device_topic, now_ms


def build_command(device_id: str, action: str, params: dict,
                  user: str, ttl_ms: int = 30_000, seq: int = 0) -> Command:
    """Construct a valid, expiring Command for a device."""
    issued = now_ms()
    return Command(
        msg_id=uuid.uuid4().hex,
        device_id=device_id,
        command_id=uuid.uuid4().hex,
        action=action,
        params=params,
        issued_at=issued,
        expires_at=issued + ttl_ms,
        seq=seq,
        origin=Origin(user=user),
    )


class DeviceBus(Protocol):
    def send_command(self, cmd: Command, collect_ms: int = 2500) -> list[Event]:
        """Publish cmd to its device's cmd topic, return Events seen within
        collect_ms (ordered as received)."""
        ...


class MqttDeviceBus:
    """paho-mqtt implementation. One short-lived connection per call keeps the
    API stateless; fine for low command volume."""

    def __init__(self, broker: str, port: int = 1883) -> None:
        self._broker = broker
        self._port = port

    def send_command(self, cmd: Command, collect_ms: int = 2500) -> list[Event]:
        import paho.mqtt.client as mqtt  # local import so tests needn't have paho

        events: list[Event] = []
        done = threading.Event()
        evt_topic = device_topic(cmd.device_id, "evt")

        def on_connect(c, u, flags, rc, props=None):
            c.subscribe(evt_topic, qos=1)
            c.publish(device_topic(cmd.device_id, "cmd"), cmd.model_dump_json(), qos=1)

        def on_message(c, u, msg):
            try:
                ev = Event.model_validate_json(msg.payload)
            except Exception:  # noqa: BLE001 - ignore anything that isn't our Event
                return
            if ev.command_id == cmd.command_id:
                events.append(ev)
                # completed/failed/expired/rejected are terminal for this command
                if ev.state.value in ("completed", "failed", "expired", "rejected"):
                    done.set()

        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                             client_id=f"eco-api-{cmd.command_id[:8]}")
        client.on_connect = on_connect
        client.on_message = on_message
        client.connect(self._broker, self._port, keepalive=15)
        client.loop_start()
        done.wait(timeout=collect_ms / 1000.0)
        client.loop_stop()
        client.disconnect()
        return events
