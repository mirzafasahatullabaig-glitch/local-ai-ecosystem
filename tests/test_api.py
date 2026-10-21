import httpx
import pytest
from fastapi.testclient import TestClient

from backend.api.main import create_app
from backend.model_gateway.gateway import ModelGateway
from backend.storage.sqlite_repo import SQLiteRepository

OK_BODY = {
    "response": "MQTT is a messaging protocol.",
    "total_duration": 2_000_000_000,
    "load_duration": 100_000_000,
    "prompt_eval_duration": 100_000_000,
    "eval_duration": 1_000_000_000,
    "eval_count": 20,
}


@pytest.fixture
def client(tmp_path):
    repo = SQLiteRepository(str(tmp_path / "t.db"))

    def handler(req):
        if req.url.path == "/api/ps":
            return httpx.Response(200, json={"models": []})
        return httpx.Response(200, json=OK_BODY)

    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport, base_url="http://127.0.0.1:11434")
    gateway = ModelGateway(http)
    return TestClient(create_app(repo, gateway))


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_models(client):
    r = client.get("/models")
    assert r.status_code == 200
    assert "reason" in r.json()["roles"]


def test_register_and_list_device(client):
    manifest = {
        "msg_id": "m", "device_id": "arm_01", "ts": 1,
        "device_type": "arm", "hw": "rp2040", "fw_version": "0.1",
        "protocol_version": 1,
        "capabilities": [{"name": "capture_image", "max_duration_ms": 1000,
                          "physical": True, "risk": "low"}],
    }
    r = client.post("/devices/register", json=manifest)
    assert r.status_code == 200
    assert r.json()["registered"] == "arm_01"
    assert len(client.get("/devices").json()["devices"]) == 1


def test_register_bad_protocol_422(client):
    manifest = {
        "msg_id": "m", "device_id": "arm_01", "ts": 1,
        "device_type": "arm", "hw": "x", "fw_version": "0.1",
        "protocol_version": 2, "capabilities": [],
    }
    assert client.post("/devices/register", json=manifest).status_code == 422


def test_ask_deterministic_list_devices_no_model(client):
    r = client.post("/ask", json={"text": "list devices"})
    assert r.status_code == 200
    body = r.json()
    assert body["tier"] == "deterministic"
    assert body["action"] == "list_devices"
    assert "model" not in body  # no LLM was called


def test_ask_estop_no_bus_returns_501_not_success(client):
    # No bus configured -> explicit 501, never a fake success.
    r = client.post("/ask", json={"text": "estop"})
    assert r.status_code == 501
    assert "messaging layer" in r.json()["detail"]


def test_ask_llm_returns_metrics(client):
    r = client.post("/ask", json={"text": "what is MQTT"})
    assert r.status_code == 200
    body = r.json()
    assert body["tier"] == "llm"
    assert body["role"] == "reason"
    assert body["model"] == "llama3.2:3b"  # reason now uses the fast model
    assert body["metrics"]["tokens"] == 20
    # task was logged
    assert len(client.get("/tasks").json()["tasks"]) == 1


# --- device-action path with a fake bus (no real MQTT) ---

from schemas.messages import Event, State  # noqa: E402


class FakeBus:
    """Records sent commands and returns a scripted terminal event chain."""

    def __init__(self):
        self.sent = []

    def send_command(self, cmd, collect_ms=2500):
        self.sent.append(cmd)
        chain = [State.RECEIVED, State.ACCEPTED, State.EXECUTING, State.COMPLETED]
        return [Event(msg_id=f"e{i}", device_id=cmd.device_id,
                      command_id=cmd.command_id, state=s)
                for i, s in enumerate(chain)]


@pytest.fixture
def bus_client(tmp_path):
    repo = SQLiteRepository(str(tmp_path / "t.db"))

    def handler(req):
        return httpx.Response(200, json=OK_BODY)

    http = httpx.Client(transport=httpx.MockTransport(handler),
                        base_url="http://127.0.0.1:11434")
    bus = FakeBus()
    app = create_app(repo, ModelGateway(http), bus=bus)
    client = TestClient(app)
    # register a device that offers 'estop'
    client.post("/devices/register", json={
        "msg_id": "m", "device_id": "arm_01", "ts": 1,
        "device_type": "arm", "hw": "x", "fw_version": "0.1",
        "protocol_version": 1,
        "capabilities": [{"name": "estop", "max_duration_ms": 100,
                          "physical": True, "risk": "high"}],
    })
    return client, bus


def test_estop_with_bus_publishes_and_collects_events(bus_client):
    client, bus = bus_client
    r = client.post("/ask", json={"text": "estop"})
    assert r.status_code == 200
    body = r.json()
    assert body["action"] == "estop"
    assert body["results"][0]["device_id"] == "arm_01"
    assert body["results"][0]["states"] == ["received", "accepted", "executing", "completed"]
    # a real, valid, expiring Command was built and sent
    assert len(bus.sent) == 1
    sent = bus.sent[0]
    assert sent.action == "estop"
    assert sent.expires_at > sent.issued_at


def test_device_action_no_matching_device_404(bus_client):
    client, _ = bus_client
    # no device offers 'ping' -> 404, not a fake success
    r = client.post("/ask", json={"text": "ping all devices"})
    assert r.status_code == 404
