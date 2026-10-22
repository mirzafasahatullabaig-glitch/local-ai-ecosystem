"""Developer page endpoints and the API-key / page-header hardening."""
from collections import deque
import json

import httpx
import pytest
from fastapi.testclient import TestClient

import backend.api.dev as dev
from backend.api.main import create_app
from backend.model_gateway.gateway import ModelGateway
from backend.storage.sqlite_repo import SQLiteRepository
from tests.test_api import OK_BODY, FakeBus

KEY = {"X-API-Key": "k"}


def _app(tmp_path, bus=None, api_key="k"):
    def handler(req):
        if req.url.path == "/api/ps":
            return httpx.Response(200, json={"models": [
                {"name": "llama3.2:3b", "size": 2 * 2**30, "size_vram": 2 * 2**30}]})
        return httpx.Response(200, json=OK_BODY)
    http = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://127.0.0.1:11434")
    traces, traffic = deque(maxlen=100), deque(maxlen=500)
    traffic.append({"seq": 1, "ts": 1, "topic": "eco/v1/devices/x/state", "bytes": 2,
                    "retained": True, "payload": "{}"})
    app = create_app(SQLiteRepository(str(tmp_path / "t.db")), ModelGateway(http), bus=bus,
                     api_key=api_key, traffic=traffic, traces=traces)
    return TestClient(app), traces


def test_read_endpoints_need_the_key(tmp_path):
    c, _ = _app(tmp_path)
    for path in ("/devices", "/models", "/tasks", "/status", "/dev/state", "/dev/models",
                 "/dev/logs", "/dev/health"):
        assert c.get(path).status_code == 401, path
    assert c.get("/health").status_code == 200          # liveness stays open
    assert c.get("/devices", headers=KEY).status_code == 200


def test_pages_send_csp_and_hide_key_from_lan(tmp_path):
    c, _ = _app(tmp_path)
    lan, local = c, TestClient(c.app, client=("127.0.0.1", 5000))
    for path in ("/", "/dev"):
        r = lan.get(path)
        assert "connect-src 'self'" in r.headers["content-security-policy"], path
        assert "'k'" not in r.text and "= 'k'" not in r.text, path
        assert "let KEY = 'k'" in local.get(path).text, path


def test_trace_records_route_prompt_and_metrics(tmp_path):
    c, traces = _app(tmp_path)
    c.post("/ask", json={"text": "what is MQTT"}, headers=KEY)
    t = c.get("/dev/state", headers=KEY).json()["traces"][0]
    assert t["routed"] == "reason" and t["role"] == "reason" and t["model"] == "llama3.2:3b"
    assert t["prompt"] == "what is MQTT" and t["metrics"]["tokens"] == 20 and t["error"] is None


def test_forced_role_overrides_router(tmp_path):
    c, _ = _app(tmp_path)
    body = c.post("/ask", json={"text": "what is MQTT", "role": "code"}, headers=KEY).json()
    assert body["role"] == "code" and body["model"] == "qwen2.5-coder:7b"
    assert c.post("/ask", json={"text": "x", "role": "nope"}, headers=KEY).status_code == 422


def test_traffic_is_incremental(tmp_path):
    c, _ = _app(tmp_path)
    assert len(c.get("/dev/state?since=0", headers=KEY).json()["traffic"]) == 1
    assert c.get("/dev/state?since=1", headers=KEY).json()["traffic"] == []


def test_dev_models_reports_vram(tmp_path):
    c, _ = _app(tmp_path)
    m = c.get("/dev/models", headers=KEY).json()["models"][0]
    assert m["name"] == "llama3.2:3b" and m["vram_mb"] == 2048


def test_raw_command_only_advertised_actions(tmp_path):
    bus = FakeBus()
    c, _ = _app(tmp_path, bus=bus)
    c.post("/devices/register", headers=KEY, json={
        "msg_id": "m", "device_id": "esp32_dev_01", "ts": 1, "device_type": "esp32", "hw": "x",
        "fw_version": "0.4", "protocol_version": 1,
        "capabilities": [{"name": "beep", "max_duration_ms": 3000, "physical": True, "risk": "low"}]})
    r = c.post("/dev/command", headers=KEY,
               json={"device_id": "esp32_dev_01", "action": "beep", "params": {"ms": 50}})
    assert r.status_code == 200 and r.json()["states"][-1] == "completed"
    assert bus.sent[-1].params == {"ms": 50} and bus.sent[-1].origin.user == "dev"
    assert c.post("/dev/command", headers=KEY,
                  json={"device_id": "esp32_dev_01", "action": "estop"}).status_code == 422
    assert c.post("/dev/command", headers=KEY,
                  json={"device_id": "esp32_dev_01", "action": "beep",
                        "params": {"x": "y" * 3000}}).status_code == 413
    assert c.post("/dev/command", headers=KEY,
                  json={"device_id": "ghost", "action": "beep"}).status_code == 404


def test_logs_fixed_sources_only(tmp_path, monkeypatch):
    log = tmp_path / "api.log"
    log.write_text("\n".join(f"line {i}" for i in range(300)), encoding="utf-8")
    monkeypatch.setattr(dev, "LOG_FILE", str(log))
    c, _ = _app(tmp_path)
    j = c.get("/dev/logs?source=api&lines=20", headers=KEY).json()
    assert j["lines"][-1] == "line 299" and len(j["lines"]) == 20
    assert c.get("/dev/logs?source=../../etc/passwd", headers=KEY).status_code == 422
    assert c.get("/dev/logs?source=api&lines=99999", headers=KEY).status_code == 422


def test_health_shape(tmp_path, monkeypatch):
    monkeypatch.setattr(dev, "_pi_health", lambda: None)   # no ssh from tests
    monkeypatch.setattr(dev, "_gpu", lambda: None)
    c, _ = _app(tmp_path)
    h = c.get("/dev/health", headers=KEY).json()
    assert h["ollama"]["up"] is True and h["pi"] is None
    assert set(h["laptop"]) == {"cpu_pct", "ram_used_mb", "ram_total_mb", "gpu"}


def test_pi_health_parser(monkeypatch):
    sample = ("svc eco-agent active\nsvc eco-tft failed\ntemp 51234\nload 0.42\n"
              "mem 412 3796\nup 86400\n")
    monkeypatch.setattr(dev, "_ssh", lambda cmd, timeout=8: sample)
    h = dev._pi_health()
    assert h == {"services": {"eco-agent": "active", "eco-tft": "failed"}, "temp_c": 51.2,
                 "load_1m": 0.42, "mem_used_mb": 412, "mem_total_mb": 3796, "uptime_s": 86400}
