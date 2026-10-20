import httpx
import pytest
from fastapi.testclient import TestClient

from backend.api.main import create_app
from backend.model_gateway.gateway import ModelGateway, PRIMARY
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


def test_ask_estop_returns_501_not_success(client):
    r = client.post("/ask", json={"text": "estop"})
    assert r.status_code == 501
    assert "Phase 2" in r.json()["detail"]


def test_ask_llm_returns_metrics(client):
    r = client.post("/ask", json={"text": "what is MQTT"})
    assert r.status_code == 200
    body = r.json()
    assert body["tier"] == "llm"
    assert body["role"] == "reason"
    assert body["model"] == PRIMARY
    assert body["metrics"]["tokens"] == 20
    # task was logged
    assert len(client.get("/tasks").json()["tasks"]) == 1
