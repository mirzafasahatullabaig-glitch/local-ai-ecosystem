import pytest

from backend.device_registry.registry import DeviceRegistry, RegistrationError
from backend.storage.sqlite_repo import SQLiteRepository
from schemas.messages import Capability, Manifest


def _manifest(device_id="arm_01", proto=1, caps=("capture_image",), ts=1000):
    return Manifest(
        msg_id="m",
        device_id=device_id,
        ts=ts,
        device_type="arm",
        hw="rp2040",
        fw_version="0.1",
        protocol_version=proto,
        capabilities=[
            Capability(name=c, max_duration_ms=1000, physical=True, risk="low")
            for c in caps
        ],
    )


@pytest.fixture
def reg(tmp_path):
    return DeviceRegistry(SQLiteRepository(str(tmp_path / "t.db")))


def test_register_and_find_by_capability(reg):
    reg.register(_manifest(caps=("capture_image", "move")))
    reg.heartbeat("arm_01", at_ms=1000)
    found = reg.find_by_capability("capture_image", now=2000)
    assert [d["device_id"] for d in found] == ["arm_01"]
    assert reg.find_by_capability("fly", now=2000) == []


def test_bad_protocol_rejected(reg):
    with pytest.raises(RegistrationError):
        reg.register(_manifest(proto=2))


def test_stale_after_15s(reg):
    reg.register(_manifest())
    reg.heartbeat("arm_01", at_ms=1000)
    # 15s exactly is still fresh; one ms past is stale.
    assert reg.list(now=1000 + 15_000)[0]["stale"] is False
    assert reg.list(now=1000 + 15_001)[0]["stale"] is True
    # A stale device is not offered for its capability.
    assert reg.find_by_capability("capture_image", now=1000 + 15_001) == []


def test_mark_offline(reg):
    reg.register(_manifest())
    reg.heartbeat("arm_01", at_ms=1000)
    reg.mark_offline("arm_01")
    assert reg.list()[0]["online"] is False
    assert reg.find_by_capability("capture_image", now=1000) == []


def test_reregister_idempotent(reg):
    reg.register(_manifest(ts=1000))
    reg.register(_manifest(ts=2000, caps=("move",)))
    devices = reg.list(now=2000)
    assert len(devices) == 1
    assert {c["name"] for c in devices[0]["capabilities"]} == {"move"}
