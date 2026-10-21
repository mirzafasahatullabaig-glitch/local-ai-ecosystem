"""Decides how to handle a request: a deterministic command, or an LLM role.

Tier 1 is pure regex, anchored at the start of the (stripped, lowercased) text
so "please don't stop all logging" never reads as an e-stop. No LLM runs here,
and the router never touches devices or the gateway — it only returns a
decision for the caller to act on.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, Optional

Tier = Literal["deterministic", "llm"]
Role = Literal["vision", "code", "plan", "reason"]


@dataclass
class Decision:
    tier: Tier
    # deterministic:
    action: Optional[str] = None
    # llm:
    role: Optional[Role] = None


# Tier 1: anchored patterns -> action name. Order matters only for readability;
# patterns are mutually exclusive in practice. Each is anchored with \A.
_DETERMINISTIC: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\A(e-?stop|emergency stop|stop all)\b"), "estop"),
    (re.compile(r"\A(list|show) devices\b"), "list_devices"),
    (re.compile(r"\A(device status|status of)\b"), "device_status"),
    (re.compile(r"\A(list|show) models\b"), "list_models"),
    (re.compile(r"\A(capture|take)( an?)? (image|photo|picture)\b"), "capture_image"),
    (re.compile(r"\Aping( all)?( devices)?\b"), "ping"),
]

# Tier 2 role hints (substring is fine here: these only pick a model, they do
# not trigger a physical action, so a false positive is cheap).
_CODE_HINT = re.compile(r"\b(code|function|python|bug|refactor|script|compile)\b")
_VISION_HINT = re.compile(r"\b(image|photo|picture|see|look|camera|visual)\b")
_PLAN_HINT = re.compile(r"\b(plan|steps|then|after that|sequence|multi-step)\b")


def route(text: str) -> Decision:
    """Classify one request into a Decision."""
    t = text.strip().lower()

    for pattern, action in _DETERMINISTIC:
        if pattern.search(t):
            return Decision(tier="deterministic", action=action)

    if _CODE_HINT.search(t):
        return Decision(tier="llm", role="code")
    if _VISION_HINT.search(t):
        return Decision(tier="llm", role="vision")
    if _PLAN_HINT.search(t):
        return Decision(tier="llm", role="plan")
    return Decision(tier="llm", role="reason")
