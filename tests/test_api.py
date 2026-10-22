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


def test_api_key_enforced(tmp_path):
    repo = SQLiteRepository(str(tmp_path / "t.db"))
    http = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=OK_BODY)),
                        base_url="http://127.0.0.1:11434")
    app = create_app(repo, ModelGateway(http), api_key="secret123")
    c = TestClient(app)
    # /ask without key -> 401; with correct key -> 200
    assert c.post("/ask", json={"text": "what is MQTT"}).status_code == 401
    assert c.post("/ask", json={"text": "what is MQTT"},
                  headers={"X-API-Key": "secret123"}).status_code == 200
    # read-only /health stays open
    assert c.get("/health").status_code == 200


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


# --- LLM prompt context, voice style, TFT mirror ---

import time  # noqa: E402

import backend.api.main as api_main  # noqa: E402
from schemas.messages import now_ms  # noqa: E402


def _capturing_app(tmp_path, sensors=None, bus=None, answer="ok"):
    prompts = []

    def handler(req):
        if req.url.path == "/api/ps":
            return httpx.Response(200, json={"models": []})
        import json
        prompts.append(json.loads(req.content)["prompt"])
        return httpx.Response(200, json={**OK_BODY, "response": answer})

    http = httpx.Client(transport=httpx.MockTransport(handler),
                        base_url="http://127.0.0.1:11434")
    app = create_app(SQLiteRepository(str(tmp_path / "t.db")), ModelGateway(http),
                     bus=bus, sensors=sensors)
    return TestClient(app), prompts


def test_llm_prompt_includes_fresh_room_reading(tmp_path):
    sensors = {"temperature_c": 24.5, "humidity": 54, "ts": now_ms()}
    c, prompts = _capturing_app(tmp_path, sensors=sensors)
    assert c.post("/ask", json={"text": "is it warm in here"}).status_code == 200
    assert "24.5°C" in prompts[0] and "54% humidity" in prompts[0]
    assert prompts[0].endswith("is it warm in here")
    assert api_main.VOICE_STYLE not in prompts[0]   # /ask keeps full answers


def test_llm_prompt_skips_stale_or_missing_reading(tmp_path):
    stale = {"temperature_c": 30.0, "humidity": None, "ts": now_ms() - 3_600_000}
    c, prompts = _capturing_app(tmp_path, sensors=stale)
    c.post("/ask", json={"text": "what is MQTT"})
    assert prompts[0] == "what is MQTT"


def test_listen_uses_voice_style(tmp_path, monkeypatch):
    seg = type("Seg", (), {"text": " what is MQTT "})
    fake = type("STT", (), {"transcribe": lambda self, p, beam_size=1: ([seg], None)})()
    monkeypatch.setattr(api_main, "_STT", fake)
    c, prompts = _capturing_app(tmp_path)
    r = c.post("/listen", files={"audio": ("clip.wav", b"RIFF0000", "audio/wav")})
    assert r.status_code == 200 and r.json()["transcript"] == "what is MQTT"
    assert prompts[0].startswith(api_main.VOICE_STYLE)


def test_answer_mirrored_trimmed_to_tft(tmp_path):
    bus = FakeBus()
    long = "word " * 60
    c, _ = _capturing_app(tmp_path, bus=bus, answer=long)
    c.post("/devices/register", json={
        "msg_id": "m", "device_id": "raspberrypi", "ts": 1, "device_type": "pi",
        "hw": "x", "fw_version": "0.1", "protocol_version": 1,
        "capabilities": [{"name": "tft_show", "max_duration_ms": 1000,
                          "physical": True, "risk": "none"}]})
    r = c.post("/ask", json={"text": "what is MQTT"})
    assert r.json()["answer"] == long          # HTTP answer stays full
    for _ in range(50):                        # mirror runs on a background thread
        if bus.sent:
            break
        time.sleep(0.02)
    text = bus.sent[0].params["text"]
    assert bus.sent[0].action == "tft_show"
    assert len(text) <= 114 and text.endswith(" ...")


def test_room_question_triggers_fresh_dht_read(tmp_path):
    sensors = {}

    class DhtBus(FakeBus):
        # stands in for the DeviceListener catching the read_dht result evt
        def send_command(self, cmd, collect_ms=2500):
            if cmd.action == "read_dht":
                sensors.update(temperature_c=26.0, humidity=50, ts=now_ms())
            return super().send_command(cmd, collect_ms)

    bus = DhtBus()
    c, prompts = _capturing_app(tmp_path, sensors=sensors, bus=bus)
    c.post("/devices/register", json={
        "msg_id": "m", "device_id": "esp32_dev_01", "ts": 1, "device_type": "esp32",
        "hw": "x", "fw_version": "0.3", "protocol_version": 1,
        "capabilities": [{"name": "read_dht", "max_duration_ms": 1000,
                          "physical": False, "risk": "none"}]})
    c.post("/ask", json={"text": "what is MQTT"})          # not a room question
    assert not any(s.action == "read_dht" for s in bus.sent)
    c.post("/ask", json={"text": "what is the temperature"})
    assert any(s.action == "read_dht" for s in bus.sent)
    assert "26.0°C" in prompts[-1]


# --- web panel: /status, /devices/{id}/action, /ask/stream ---

def _register(c, device_id, caps):
    c.post("/devices/register", json={
        "msg_id": "m", "device_id": device_id, "ts": 1, "device_type": "x",
        "hw": "x", "fw_version": "0.1", "protocol_version": 1,
        "capabilities": [{"name": n, "max_duration_ms": 1000, "physical": True,
                          "risk": "low"} for n in caps]})


def test_status_reports_mood_room_and_last_answer(tmp_path):
    from collections import deque
    sensors = {"temperature_c": 34.0, "humidity": 40, "ts": now_ms()}
    log = deque([{"ts": 1, "device": "esp32_dev_01", "text": "completed"}])
    repo = SQLiteRepository(str(tmp_path / "t.db"))
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=OK_BODY)),
                        base_url="http://127.0.0.1:11434")
    c = TestClient(create_app(repo, ModelGateway(http), sensors=sensors, event_log=log))
    _register(c, "esp32_dev_01", ["read_dht"])
    s = c.get("/status").json()
    assert s["mood"] == "alert"                       # 34°C >= 33°C threshold
    assert s["sensors"]["temperature_c"] == 34.0
    assert s["log"][0]["text"] == "completed"
    sensors["temperature_c"] = 22.0
    c.post("/ask", json={"text": "what is MQTT"})
    s = c.get("/status").json()
    assert s["mood"] == "speaking" and s["last"]["text"] == "what is MQTT"


def test_panel_action_whitelist_and_capability_checks(bus_client):
    client, bus = bus_client                           # arm_01 offers only estop
    _register(client, "esp32_dev_01", ["beep", "update"])
    r = client.post("/devices/esp32_dev_01/action", json={"action": "beep"})
    assert r.status_code == 200 and r.json()["states"][-1] == "completed"
    assert bus.sent[-1].params == {"ms": 300}          # server picks the params
    # not on the panel whitelist even though the device offers it
    assert client.post("/devices/esp32_dev_01/action",
                       json={"action": "update"}).status_code == 422
    # whitelisted, but this device doesn't offer it
    assert client.post("/devices/esp32_dev_01/action",
                       json={"action": "blink_led"}).status_code == 422
    assert client.post("/devices/nope/action", json={"action": "ping"}).status_code == 404
    n = len(bus.sent)
    assert client.post("/devices/arm_01/action", json={"action": "estop"}).status_code == 422
    assert len(bus.sent) == n                          # nothing reached a device


def test_panel_key_only_embedded_for_loopback(tmp_path):
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=OK_BODY)),
                        base_url="http://127.0.0.1:11434")
    app = create_app(SQLiteRepository(str(tmp_path / "t.db")), ModelGateway(http),
                     api_key="secret123")
    assert "secret123" not in TestClient(app).get("/").text            # LAN client
    assert "secret123" in TestClient(app, client=("127.0.0.1", 5000)).get("/").text


def _ndjson(*objs):
    import json
    return "\n".join(json.dumps(o) for o in objs).encode()


def test_ask_stream_sends_chunks_then_done(tmp_path):
    def handler(req):
        if req.url.path == "/api/ps":
            return httpx.Response(200, json={"models": []})
        return httpx.Response(200, content=_ndjson(
            {"response": "MQTT "}, {"response": "is a protocol."},
            {"response": "", "done": True, "eval_count": 2, "eval_duration": 1_000_000_000,
             "total_duration": 2_000_000_000}))
    http = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://127.0.0.1:11434")
    c = TestClient(create_app(SQLiteRepository(str(tmp_path / "t.db")), ModelGateway(http)))
    import json
    events = [json.loads(l[6:]) for l in c.post("/ask/stream", json={"text": "what is MQTT"})
              .text.split("\n\n") if l.startswith("data: ")]
    assert [e["t"] for e in events] == ["chunk", "chunk", "done"]
    assert events[-1]["answer"] == "MQTT is a protocol."
    assert events[-1]["metrics"]["tokens"] == 2
    assert len(c.get("/tasks").json()["tasks"]) == 1    # logged like /ask


def test_ask_stream_deterministic_is_one_event(client):
    import json
    body = client.post("/ask/stream", json={"text": "list devices"}).text
    events = [json.loads(l[6:]) for l in body.split("\n\n") if l.startswith("data: ")]
    assert len(events) == 1 and events[0]["action"] == "list_devices"


def test_gateway_stream_falls_back_before_first_piece():
    calls = []

    def handler(req):
        import json
        if req.url.path == "/api/ps":
            return httpx.Response(200, json={"models": []})
        model = json.loads(req.content).get("model")
        calls.append(model)
        if model == "llama3.2:3b" and json.loads(req.content).get("stream"):
            return httpx.Response(500)                   # primary fails up front
        return httpx.Response(200, content=_ndjson({"response": "ok", "done": True}))
    gw = ModelGateway(httpx.Client(transport=httpx.MockTransport(handler),
                                   base_url="http://127.0.0.1:11434"))
    gen = gw.stream("reason", "hi")
    pieces = []
    try:
        while True:
            pieces.append(next(gen))
    except StopIteration as stop:
        result = stop.value
    assert pieces == ["ok"] and result.model == "qwen3.5:4b" and result.fell_back


def test_room_fact_only_on_room_questions(tmp_path):
    sensors = {"temperature_c": 24.5, "humidity": 54, "ts": now_ms()}
    c, prompts = _capturing_app(tmp_path, sensors=sensors)
    c.post("/ask", json={"text": "what is 2 plus 2"})
    assert prompts[-1] == "what is 2 plus 2"        # fresh reading, but not asked about
