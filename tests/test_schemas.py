import pytest
from pydantic import ValidationError

from schemas.messages import (
    Command,
    Envelope,
    Origin,
    State,
    can_transition,
    device_topic,
)


def _cmd(**over):
    base = dict(
        msg_id="m1",
        device_id="arm_01",
        command_id="c1",
        action="move",
        issued_at=1000,
        expires_at=2000,
        seq=0,
        origin=Origin(user="u"),
    )
    base.update(over)
    return Command(**base)


def test_bad_device_id_rejected():
    for bad in ["", "ab", "UPPER", "has space", "x" * 33, "bad-dash"]:
        with pytest.raises(ValidationError):
            Envelope(msg_id="m", device_id=bad)


def test_good_device_id_accepted():
    assert Envelope(msg_id="m", device_id="arm_01").device_id == "arm_01"


def test_expires_before_issued_rejected():
    with pytest.raises(ValidationError):
        _cmd(issued_at=2000, expires_at=2000)  # equal is not later
    with pytest.raises(ValidationError):
        _cmd(issued_at=2000, expires_at=1000)


def test_is_expired():
    c = _cmd(issued_at=1000, expires_at=2000)
    assert c.is_expired(at_ms=2000) is True
    assert c.is_expired(at_ms=2500) is True
    assert c.is_expired(at_ms=1999) is False


def test_illegal_transitions_blocked():
    assert can_transition(State.RECEIVED, State.COMPLETED) is False
    assert can_transition(State.COMPLETED, State.EXECUTING) is False
    assert can_transition(State.FAILED, State.EXECUTING) is False


def test_legal_transitions_allowed():
    assert can_transition(State.RECEIVED, State.ACCEPTED) is True
    assert can_transition(State.ACCEPTED, State.EXECUTING) is True
    assert can_transition(State.EXECUTING, State.COMPLETED) is True
    assert can_transition(State.EXECUTING, State.FAILED) is True


def test_topic_helper():
    assert device_topic("arm_01", "cmd") == "eco/v1/devices/arm_01/cmd"
    with pytest.raises(ValueError):
        device_topic("arm_01", "bogus")
