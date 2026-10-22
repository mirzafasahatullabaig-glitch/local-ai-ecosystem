"""Publishes Commands to devices over MQTT and collects their Events.

A DeviceBus Protocol lets the API depend on an interface, so tests inject a
fake and pytest never touches a real broker — same pattern as the model
gateway. The MQTT implementation is only wired in the uvicorn factory.

The LLM never produces Commands. The API builds them from whitelisted
deterministic actions and sends them here.
"""
from __future__ import annotations

import json
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

    def __init__(self, broker: str, port: int = 1883,
                 user: str | None = None, password: str | None = None) -> None:
        self._broker = broker
        self._port = port
        self._user = user
        self._password = password

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
        if self._user:
            client.username_pw_set(self._user, self._password)
        client.on_connect = on_connect
        client.on_message = on_message
        client.connect(self._broker, self._port, keepalive=15)
        client.loop_start()
        done.wait(timeout=collect_ms / 1000.0)
        client.loop_stop()
        client.disconnect()
        return events


class DeviceListener:
    """Background MQTT subscriber that keeps the registry live: devices that
    announce a manifest are registered; a manifest or state message refreshes
    their heartbeat. Lets devices self-register instead of the laptop POSTing.
    If `sensors` is given, the latest DHT reading seen on any evt is kept in it
    so the API can tell the LLM about the room. If `log` (a bounded deque) is
    given, announcements, command events and offline notices are appended to
    it for the web panel — heartbeats are not, they'd drown everything else.
    """

    def __init__(self, registry, broker: str, port: int = 1883,
                 user: str | None = None, password: str | None = None,
                 sensors: dict | None = None, log=None) -> None:
        self._registry = registry
        self._sensors = sensors
        self._log = log
        self._broker = broker
        self._port = port
        self._user = user
        self._password = password
        self._client = None

    def start(self) -> None:
        import paho.mqtt.client as mqtt
        from schemas.messages import Manifest, now_ms

        def note(device_id: str, text: str) -> None:
            if self._log is not None:
                self._log.append({"ts": now_ms(), "device": device_id, "text": text})

        def on_connect(c, u, flags, rc, props=None):
            c.subscribe("eco/v1/devices/+/manifest", qos=1)
            c.subscribe("eco/v1/devices/+/state", qos=1)
            if self._sensors is not None or self._log is not None:
                c.subscribe("eco/v1/devices/+/evt", qos=1)

        def on_message(c, u, msg):
            device_id, kind = msg.topic.split("/")[3], msg.topic.rsplit("/", 1)[-1]
            if kind == "evt":
                try:
                    ev = json.loads(msg.payload)
                except ValueError:
                    return
                if not isinstance(ev, dict):
                    return
                text = str(ev.get("state", "event"))
                if "temperature_c" in ev:
                    text += f" {ev['temperature_c']}°C {ev.get('humidity')}%"
                    if self._sensors is not None:
                        self._sensors.update(temperature_c=ev["temperature_c"],
                                             humidity=ev.get("humidity"), ts=now_ms())
                note(device_id, text)
                return
            if kind == "manifest":
                try:
                    m = Manifest.model_validate_json(msg.payload)
                    self._registry.register(m)
                    self._registry.heartbeat(m.device_id)
                    note(m.device_id, f"announced fw {m.fw_version}")
                except Exception:  # noqa: BLE001 - ignore malformed announcements
                    pass
            elif kind == "state":
                try:
                    online = json.loads(msg.payload).get("online", True)
                except (ValueError, AttributeError):
                    online = True
                if online:
                    self._registry.heartbeat(device_id, now_ms())
                else:   # last-will: the broker saw the device drop
                    self._registry.mark_offline(device_id)
                    note(device_id, "went offline")

        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                   client_id="eco-api-listener")
        if self._user:
            self._client.username_pw_set(self._user, self._password)
        self._client.on_connect = on_connect
        self._client.on_message = on_message
        self._client.connect(self._broker, self._port, keepalive=30)
        self._client.loop_start()

    def stop(self) -> None:
        if self._client is not None:
            self._client.loop_stop()
            self._client.disconnect()
