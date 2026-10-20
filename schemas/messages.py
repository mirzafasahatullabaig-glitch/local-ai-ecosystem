"""Wire protocol for the ecosystem (schema_version 1).

All messages share an Envelope. Devices announce a Manifest, the orchestrator
issues Commands, and devices reply with Events whose state follows a fixed
machine. Nothing here talks to Ollama, SQLite or the network — it is pure data.
"""
from __future__ import annotations

import time
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

SCHEMA_VERSION = 1


def now_ms() -> int:
    """Current wall-clock time in milliseconds."""
    return int(time.time() * 1000)


# --- Envelope ---------------------------------------------------------------

DEVICE_ID_PATTERN = r"^[a-z0-9_]{3,32}$"


class Envelope(BaseModel):
    """Fields every message carries."""

    schema_version: Literal[1] = SCHEMA_VERSION
    msg_id: str
    device_id: str = Field(pattern=DEVICE_ID_PATTERN)
    ts: int = Field(default_factory=now_ms, description="emit time, ms since epoch")


# --- Manifest ---------------------------------------------------------------


class CapabilityParam(BaseModel):
    type: str
    min: Optional[float] = None
    max: Optional[float] = None


class Capability(BaseModel):
    name: str
    params: dict[str, CapabilityParam] = Field(default_factory=dict)
    max_duration_ms: int = Field(gt=0)
    physical: bool
    risk: Literal["none", "low", "medium", "high"]


class Manifest(Envelope):
    """A device's self-description, sent on connect."""

    device_type: str
    hw: str
    fw_version: str
    protocol_version: int
    capabilities: list[Capability] = Field(default_factory=list)


# --- Command ----------------------------------------------------------------


class Origin(BaseModel):
    user: str
    task_id: Optional[str] = None


class Command(Envelope):
    """An instruction for one device. Expires; never retried blindly."""

    command_id: str
    action: str
    params: dict = Field(default_factory=dict)
    issued_at: int
    expires_at: int
    seq: int = Field(ge=0)
    origin: Origin
    sig: Optional[str] = None

    @model_validator(mode="after")
    def _expiry_after_issue(self) -> "Command":
        if self.expires_at <= self.issued_at:
            raise ValueError("expires_at must be later than issued_at")
        return self

    def is_expired(self, at_ms: Optional[int] = None) -> bool:
        """True once the command's deadline has passed."""
        return (at_ms if at_ms is not None else now_ms()) >= self.expires_at


# --- Event ------------------------------------------------------------------


class State(str, Enum):
    RECEIVED = "received"
    ACCEPTED = "accepted"
    EXECUTING = "executing"
    COMPLETED = "completed"
    REJECTED = "rejected"
    FAILED = "failed"
    EXPIRED = "expired"


# Allowed next-states. received->accepted->executing->completed|failed is the
# happy path; a command may be rejected or expire before it starts.
_TRANSITIONS: dict[State, set[State]] = {
    State.RECEIVED: {State.ACCEPTED, State.REJECTED, State.EXPIRED},
    State.ACCEPTED: {State.EXECUTING, State.FAILED, State.EXPIRED},
    State.EXECUTING: {State.COMPLETED, State.FAILED, State.EXPIRED},
    State.COMPLETED: set(),
    State.REJECTED: set(),
    State.FAILED: set(),
    State.EXPIRED: set(),
}


def can_transition(src: State, dst: State) -> bool:
    """Whether a command may move from state ``src`` to state ``dst``."""
    return dst in _TRANSITIONS[State(src)]


class Event(Envelope):
    """A device's report on a command's progress."""

    command_id: str
    state: State


# --- Topics -----------------------------------------------------------------

_DEVICE_SUBTOPICS = {"state", "manifest", "telemetry", "cmd", "evt"}
ESTOP_TOPIC = "eco/v1/system/estop"


def device_topic(device_id: str, kind: str) -> str:
    """MQTT-style topic for a device, e.g. eco/v1/devices/<id>/cmd.

    kind is one of state|manifest|telemetry|cmd|evt.
    """
    if kind not in _DEVICE_SUBTOPICS:
        raise ValueError(f"unknown topic kind: {kind}")
    return f"eco/v1/devices/{device_id}/{kind}"
