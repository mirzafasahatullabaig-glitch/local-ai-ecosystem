"""HTTP surface. Built via create_app(repo, gateway) so tests inject fakes.

The router decides tier/role; deterministic read-only actions are answered
here, but anything needing a device (estop, capture) returns an explicit
Phase-2 error instead of a fake success. Every /ask is logged with tier,
model and metrics.
"""
from __future__ import annotations

import logging
import os
import tempfile
import threading
import time
import uuid

import httpx
from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

# Lazy singleton: faster-whisper model load is slow, do it once. The uvicorn
# factory also warms it in the background; the lock stops a double load if a
# /listen arrives mid-warm-up.
_STT = None
_STT_LOCK = threading.Lock()


def _get_stt():
    global _STT
    with _STT_LOCK:
        if _STT is None:
            from faster_whisper import WhisperModel
            size = os.environ.get("ECO_STT_MODEL", "base")
            # Not "auto": it picks CUDA, but cuBLAS isn't set up on this laptop
            # (CUBLAS_STATUS_NOT_SUPPORTED). Set ECO_STT_DEVICE=cuda once it is.
            device = os.environ.get("ECO_STT_DEVICE", "cpu")
            # int8 is fast and light on CPU and on CUDA alike.
            _STT = WhisperModel(size, device=device, compute_type="int8")
    return _STT


SENSOR_MAX_AGE_MS = 10 * 60_000   # older room readings aren't worth telling the LLM
SENSOR_REFRESH_MS = 60_000        # room questions re-read the DHT if older than this
_ROOM_WORDS = ("temp", "hot", "cold", "warm", "humid", "weather", "room")
# Don't mention the screen here: llama3.2:3b parrots it back into the answer.
VOICE_STYLE = "Reply in one or two short sentences."


def _llm_prompt(text: str, sensors: dict | None, voice: bool) -> str:
    """Prefix the user's text with live room context and, for voice, a brevity
    instruction. Routing and task logs still use the raw text."""
    parts = []
    if voice:
        parts.append(VOICE_STYLE)
    if sensors and sensors.get("temperature_c") is not None \
            and now_ms() - sensors.get("ts", 0) < SENSOR_MAX_AGE_MS:
        # Stated as a plain fact: a 3B model ignored a softer "sensor reading" hint.
        line = (f"Fact: the temperature in the user's room right now is "
                f"{sensors['temperature_c']:.1f}°C")
        if sensors.get("humidity") is not None:
            line += f" with {sensors['humidity']:.0f}% humidity"
        parts.append(line + ". If asked about temperature or the room, answer with it.")
    parts.append(text)
    return "\n".join(parts)

from backend.device_registry.registry import DeviceRegistry, RegistrationError
from backend.messaging.bus import DeviceBus, build_command
from backend.model_gateway.gateway import GatewayError, ModelGateway, ROLE_MODELS
from backend.orchestrator.router import route
from backend.storage.sqlite_repo import Repository, SQLiteRepository
from schemas.messages import Manifest, now_ms

log = logging.getLogger("eco.api")

# Deterministic actions that act on devices -> (device action, capability to
# match, params). The capability is how we pick devices; never by name. The LLM
# never populates this table — it is fixed, whitelisted, server-side.
_DEVICE_ACTIONS = {
    "estop": ("estop", "estop", {}),
    "capture_image": ("capture_image", "capture_image", {}),
    "ping": ("ping", "ping", {}),
}


class AskRequest(BaseModel):
    text: str


_INDEX_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>local-ai-ecosystem</title>
<style>
  :root { --bg:#0f1115; --card:#1a1d24; --fg:#e6e6e6; --mut:#8b93a1;
          --ok:#3ecf8e; --bad:#e5534b; --accent:#5b9bff; }
  * { box-sizing:border-box; } body { margin:0; background:var(--bg); color:var(--fg);
      font:15px/1.5 system-ui,sans-serif; padding:16px; }
  h1 { font-size:20px; margin:0 0 4px; } .mut { color:var(--mut); font-size:13px; }
  .card { background:var(--card); border-radius:10px; padding:14px; margin:12px 0; }
  .dev { display:flex; justify-content:space-between; padding:6px 0;
         border-bottom:1px solid #2a2e37; } .dev:last-child { border:0; }
  .dot { width:9px; height:9px; border-radius:50%; display:inline-block; margin-right:6px; }
  .live { background:var(--ok); } .stale { background:var(--bad); }
  input,button { font:inherit; } input[type=text] { width:100%; padding:10px;
    border-radius:8px; border:1px solid #2a2e37; background:#0c0e12; color:var(--fg); }
  button { padding:9px 14px; border-radius:8px; border:0; background:var(--accent);
    color:#fff; cursor:pointer; margin:6px 6px 0 0; } button.alt { background:#2a2e37; }
  pre { background:#0c0e12; padding:12px; border-radius:8px; overflow:auto;
    white-space:pre-wrap; word-break:break-word; max-height:50vh; }
  .states span { display:inline-block; padding:2px 7px; margin:2px; border-radius:6px;
    background:#23303f; font-size:12px; }
</style></head><body>
<h1>local-ai-ecosystem</h1>
<div class="mut">Phase 2 control panel &middot; talks to the live API and devices</div>

<div class="card"><b>Devices</b> <span class="mut" id="devcount"></span>
  <div id="devices" class="mut">loading&hellip;</div></div>

<div class="card">
  <input id="ask" type="text" placeholder='Ask something, or try "ping all devices"'
         value="what is MQTT">
  <div>
    <button onclick="send()">Ask</button>
    <button class="alt" onclick="quick('ping all devices')">Ping all</button>
    <button class="alt" onclick="quick('list devices')">List devices</button>
    <button class="alt" onclick="quick('estop')">E-stop</button>
  </div>
</div>

<div class="card"><b>Result</b><pre id="out">—</pre></div>

<script>
async function loadDevices() {
  try {
    const r = await fetch('/devices'); const j = await r.json();
    const el = document.getElementById('devices');
    document.getElementById('devcount').textContent = '(' + j.devices.length + ')';
    if (!j.devices.length) { el.textContent = 'none registered'; return; }
    el.innerHTML = j.devices.map(d => {
      const live = !d.stale && d.online;
      const caps = (d.capabilities||[]).map(c => c.name).join(', ');
      return `<div class="dev"><span><span class="dot ${live?'live':'stale'}"></span>
        <b>${d.device_id}</b> <span class="mut">${d.device_type||''} &middot; ${caps}</span></span>
        <span class="mut">${live?'live':'stale'}</span></div>`;
    }).join('');
  } catch(e) { document.getElementById('devices').textContent = 'API unreachable'; }
}
function quick(t){ document.getElementById('ask').value = t; send(); }
async function send() {
  const text = document.getElementById('ask').value;
  const out = document.getElementById('out'); out.textContent = 'sending…';
  try {
    const r = await fetch('/ask', {method:'POST',
        headers:{'Content-Type':'application/json', 'X-API-Key':'__API_KEY__'},
        body: JSON.stringify({text})});
    const j = await r.json();
    if (j.answer) { out.textContent = j.answer + '\\n\\n[' + j.model + ', ' +
        j.metrics.tokens_per_s + ' tok/s]'; }
    else if (j.results) {
      out.innerHTML = j.results.map(x => `<b>${x.device_id}</b><div class="states">` +
        x.states.map(s=>`<span>${s}</span>`).join('') + '</div>').join('');
    } else { out.textContent = JSON.stringify(j, null, 2); }
  } catch(e) { out.textContent = 'error: ' + e; }
  loadDevices();
}
loadDevices(); setInterval(loadDevices, 3000);
</script></body></html>"""


def create_app(repo: Repository, gateway: ModelGateway,
               bus: "DeviceBus | None" = None,
               api_key: str | None = None,
               sensors: dict | None = None) -> FastAPI:
    app = FastAPI(title="local-ai-ecosystem", version="1")
    registry = DeviceRegistry(repo)

    def require_key(x_api_key: str | None = Header(default=None)) -> None:
        # No key configured -> open (dev default). Configured -> must match.
        if api_key and x_api_key != api_key:
            raise HTTPException(status_code=401, detail="invalid or missing X-API-Key")

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        # Embed the key so the same-origin page can call /ask. localhost only.
        return _INDEX_HTML.replace("__API_KEY__", api_key or "")

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "ts": now_ms()}

    @app.get("/models")
    def models() -> dict:
        return {"roles": {r: list(m) for r, m in ROLE_MODELS.items()}}

    @app.get("/devices")
    def devices() -> dict:
        return {"devices": registry.list()}

    @app.post("/devices/register", dependencies=[Depends(require_key)])
    def register(manifest: Manifest) -> dict:
        try:
            dev = registry.register(manifest)
        except RegistrationError as e:
            raise HTTPException(status_code=422, detail=str(e))
        registry.heartbeat(manifest.device_id)
        return {"registered": dev["device_id"]}

    @app.get("/tasks")
    def tasks() -> dict:
        return {"tasks": repo.list_tasks()}

    def _dispatch_device_action(action: str) -> dict:
        dev_action, capability, params = _DEVICE_ACTIONS[action]
        if bus is None:
            raise HTTPException(status_code=501,
                                detail=f"{action} requires messaging layer (no bus configured)")
        # shortcut: agents don't send periodic heartbeats yet, so a device goes
        # stale 15s after register. Add a heartbeat endpoint + agent-side pings
        # before relying on this for long-lived devices.
        targets = registry.find_by_capability(capability)
        if not targets:
            raise HTTPException(status_code=404,
                                detail=f"no live device offers capability {capability!r}")
        results = []
        for i, dev in enumerate(targets):
            cmd = build_command(dev["device_id"], dev_action, params,
                                user="api", seq=i)
            events = bus.send_command(cmd)
            states = [e.state.value for e in events]
            log.info("ask action=%s device=%s states=%s",
                     action, dev["device_id"], states)
            results.append({"device_id": dev["device_id"],
                            "command_id": cmd.command_id, "states": states})
        return {"tier": "deterministic", "action": action,
                "capability": capability, "results": results}

    def _mirror_to_tft(answer: str) -> None:
        # The TFT MSG band fits ~4 short lines; send a trimmed answer so it
        # isn't clipped mid-word. send_command waits up to 2.5s for the Pi's
        # ack, so this runs off the request thread.
        tft_text = " ".join(answer.split())
        if len(tft_text) > 110:
            tft_text = tft_text[:110].rsplit(" ", 1)[0] + " ..."
        for dev in registry.find_by_capability("tft_show"):
            try:
                cmd = build_command(dev["device_id"], "tft_show",
                                    {"text": tft_text}, user="api")
                bus.send_command(cmd)
            except Exception as e:  # noqa: BLE001 - display is best-effort
                log.warning("tft_show mirror to %s failed: %s", dev["device_id"], e)

    def _refresh_room_reading(text: str) -> None:
        """The ESP only reports temperature when asked, so for a room question
        with no recent reading, send read_dht. The DeviceListener sees the
        result evt on its own connection and fills `sensors`."""
        if bus is None or sensors is None:
            return
        if not any(w in text.lower() for w in _ROOM_WORDS):
            return
        if now_ms() - sensors.get("ts", 0) < SENSOR_REFRESH_MS:
            return
        for dev in registry.find_by_capability("read_dht"):
            before = sensors.get("ts", 0)
            try:
                bus.send_command(build_command(dev["device_id"], "read_dht", {},
                                               user="api"))
            except Exception as e:  # noqa: BLE001 - a missing reading isn't fatal
                log.warning("read_dht on %s failed: %s", dev["device_id"], e)
                continue
            for _ in range(10):   # listener may lag the bus by a few ms
                if sensors.get("ts", 0) != before:
                    return
                time.sleep(0.05)

    def _answer(text: str, voice: bool = False) -> dict:
        decision = route(text)

        if decision.tier == "deterministic":
            action = decision.action
            if action == "list_devices":
                log.info("ask tier=deterministic action=list_devices")
                return {"tier": "deterministic", "action": action,
                        "devices": registry.list()}
            if action == "list_models":
                log.info("ask tier=deterministic action=list_models")
                return {"tier": "deterministic", "action": action,
                        "roles": {r: list(m) for r, m in ROLE_MODELS.items()}}
            if action in _DEVICE_ACTIONS:
                return _dispatch_device_action(action)
            log.info("ask tier=deterministic action=%s -> unsupported", action)
            raise HTTPException(status_code=501,
                                detail=f"{action} not implemented")

        # LLM tier
        task_id = uuid.uuid4().hex
        _refresh_room_reading(text)
        try:
            result = gateway.generate(decision.role, _llm_prompt(text, sensors, voice))
        except GatewayError as e:
            log.warning("ask tier=llm role=%s FAILED: %s", decision.role, e)
            raise HTTPException(status_code=502, detail=str(e))

        m = result.metrics
        log.info(
            "ask tier=llm role=%s model=%s fell_back=%s total_ms=%s tok/s=%s",
            decision.role, result.model, result.fell_back, m.total_ms, m.tokens_per_s,
        )
        repo.add_task(task_id, {"created_at": now_ms(), "text": text,
                                "role": decision.role, "model": result.model})
        # Mirror the answer to any device that can show it (the Pi TFT). Reuses
        # the proven tft_show path; best-effort, never delays the HTTP answer.
        if bus is not None:
            threading.Thread(target=_mirror_to_tft, args=(result.text,),
                             daemon=True).start()
        return {
            "tier": "llm",
            "role": decision.role,
            "model": result.model,
            "fell_back": result.fell_back,
            "answer": result.text,
            "metrics": m.__dict__,
            "task_id": task_id,
        }

    @app.post("/ask", dependencies=[Depends(require_key)])
    def ask(req: AskRequest) -> dict:
        return _answer(req.text)

    @app.post("/listen", dependencies=[Depends(require_key)])
    def listen(audio: UploadFile = File(...)) -> dict:
        """Voice entry: the Pi uploads a short WAV, we transcribe it with
        faster-whisper and run it through the same /ask logic (so the TFT mirror
        fires too). Sync def on purpose: FastAPI runs it in a worker thread, so
        slow STT + LLM don't freeze /health, /devices and the web UI."""
        model = _get_stt()
        data = audio.file.read()
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(data)
            path = f.name
        try:
            segments, _ = model.transcribe(path, beam_size=1)
            text = " ".join(s.text for s in segments).strip()
        finally:
            os.unlink(path)
        log.info("listen transcribed %d bytes -> %r", len(data), text)
        if not text:
            return {"transcript": "", "answer": None}
        result = _answer(text, voice=True)
        result["transcript"] = text
        return result

    return app


def _build_default_app() -> FastAPI:
    """Wire real SQLite + Ollama for uvicorn. Not used by tests."""
    db = os.environ.get("ECO_DB_PATH", "eco.db")
    host = os.environ.get("OLLAMA_HOST", "127.0.0.1:11434")
    host = host.replace("http://", "").replace("https://", "")  # env var may carry a scheme
    client = httpx.Client(base_url=f"http://{host}")
    # Image support on the text primary is decided by verify_models.ps1; default
    # to False so vision routes to the real vision model unless proven otherwise.
    supports = os.environ.get("VISION_PRIMARY_IMAGES", "0") == "1"
    gateway = ModelGateway(client, vision_primary_supports_images=supports)
    # Broker on the Pi by default; override with ECO_BROKER.
    broker = os.environ.get("ECO_BROKER", "YOUR_PI_IP")
    port = int(os.environ.get("ECO_BROKER_PORT", "1883"))
    mq_user = os.environ.get("ECO_MQTT_USER")
    mq_pass = os.environ.get("ECO_MQTT_PASS")
    from backend.messaging.bus import DeviceListener, MqttDeviceBus
    bus = MqttDeviceBus(broker, port, mq_user, mq_pass)
    sensors: dict = {}   # filled by the listener, read by the LLM prompt
    app = create_app(SQLiteRepository(db), gateway, bus=bus,
                     api_key=os.environ.get("ECO_API_KEY"), sensors=sensors)
    # Background listener auto-registers devices and keeps them live from their
    # retained manifest + periodic state, so the web UI shows real status.
    listener = DeviceListener(DeviceRegistry(SQLiteRepository(db)), broker, port,
                              mq_user, mq_pass, sensors=sensors)
    listener.start()
    app.state.listener = listener
    # Load Whisper now so the first voice turn isn't a multi-second cold start.
    threading.Thread(target=_get_stt, daemon=True).start()
    return app


# Built on first access (uvicorn's `backend.api.main:app` does a getattr), not at
# import — so tests importing create_app never connect to the real broker or
# load Whisper.
_APP: FastAPI | None = None


def __getattr__(name: str):
    global _APP
    if name == "app":
        if _APP is None:
            _APP = _build_default_app()
        return _APP
    raise AttributeError(name)
