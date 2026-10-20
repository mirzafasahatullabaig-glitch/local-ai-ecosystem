import pytest

from backend.orchestrator.router import route

# (text, expected tier, expected action-or-role)
CASES = [
    # --- deterministic (Tier 1) ---
    ("estop", "deterministic", "estop"),
    ("e-stop", "deterministic", "estop"),
    ("EMERGENCY STOP now", "deterministic", "estop"),
    ("stop all", "deterministic", "estop"),
    ("stop all motors", "deterministic", "estop"),
    ("list devices", "deterministic", "list_devices"),
    ("show devices please", "deterministic", "list_devices"),
    ("device status", "deterministic", "device_status"),
    ("status of arm_01", "deterministic", "device_status"),
    ("list models", "deterministic", "list_models"),
    ("show models", "deterministic", "list_models"),
    ("capture image", "deterministic", "capture_image"),
    ("take a photo", "deterministic", "capture_image"),
    ("capture an image now", "deterministic", "capture_image"),
    ("take picture", "deterministic", "capture_image"),
    # --- code role ---
    ("write a python function to sort a list", "llm", "code"),
    ("fix this bug in my script", "llm", "code"),
    ("refactor this code", "llm", "code"),
    ("why won't this compile", "llm", "code"),
    ("generate a function for fibonacci", "llm", "code"),
    # --- vision role ---
    ("what do you see in this image", "llm", "vision"),
    ("describe the photo", "llm", "vision"),
    ("look at this picture", "llm", "vision"),
    ("what is in front of the camera", "llm", "vision"),
    ("give me a visual description", "llm", "vision"),
    # --- plan role (multi-step) ---
    ("plan a route to the kitchen", "llm", "plan"),
    ("what are the steps to assemble this", "llm", "plan"),
    ("do this then that in sequence", "llm", "plan"),
    ("give me a multi-step approach", "llm", "plan"),
    # --- reason (default) ---
    ("what is MQTT", "llm", "reason"),
    ("explain how a servo works", "llm", "reason"),
    ("who are you", "llm", "reason"),
    # --- the critical negative: must NOT be estop ---
    ("please don't stop all logging", "llm", "reason"),
]


@pytest.mark.parametrize("text,tier,label", CASES)
def test_routing(text, tier, label):
    d = route(text)
    assert d.tier == tier, f"{text!r} -> wrong tier {d.tier}"
    got = d.action if tier == "deterministic" else d.role
    assert got == label, f"{text!r} -> got {got}, expected {label}"


def test_dont_stop_is_not_estop():
    # explicit, because this is the whole point of anchoring
    d = route("please don't stop all logging")
    assert d.action != "estop"
    assert d.tier == "llm"
