"""HTTP surface. Built via create_app(repo, gateway) so tests inject fakes.

The router decides tier/role; deterministic read-only actions are answered
here, but anything needing a device (estop, capture) returns an explicit
Phase-2 error instead of a fake success. Every /ask is logged with tier,
model and metrics.
"""
from __future__ import annotations

import base64
import binascii
import collections
import json
import logging
import os
import tempfile
import threading
import time
import uuid
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, Response, StreamingResponse
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
TEMP_ALERT_C = 33.0      # same threshold the ESP uses for its HOT alert
SPEAKING_MS = 90_000     # panel face shows "talking" this long after an answer


def _llm_role(role: str) -> str:
    """Role when there is NO image: a vision model would only add a ~12s swap
    to answer plain text, so "vision" falls back to reason."""
    return "reason" if role == "vision" else role


MAX_IMAGE_B64 = 8_000_000    # ~6 MB image; the panel shrinks photos to ~1024px first
MAX_AUDIO_BYTES = 10_000_000  # minutes of browser audio, far more than one question
CAMERA_WAIT_MS = 12_000       # fswebcam warm-up + the MQTT round trip
DESCRIBE = "Describe what you see in this image."


def _clean_image(b64: str | None) -> str | None:
    """Trust boundary for uploaded images: strip a data: URL prefix, cap the
    size, and require real base64 before it goes anywhere near Ollama."""
    if not b64:
        return None
    if b64.startswith("data:"):
        b64 = b64.split(",", 1)[-1]
    if len(b64) > MAX_IMAGE_B64:
        raise HTTPException(status_code=413, detail="image too large (max ~6 MB)")
    try:
        base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=422, detail="image is not valid base64")
    return b64


def _is_room_question(text: str) -> bool:
    return any(w in text.lower() for w in _ROOM_WORDS)

# Actions the web panel may send to a device. Fixed and server-side: the browser
# picks one of these names, never arbitrary actions or params. `update` (self-
# update, risk high) and estop are deliberately not here.
_PANEL_ACTIONS = {
    "ping":      lambda text: {},
    "blink_led": lambda text: {"ms": 500},
    "beep":      lambda text: {"ms": 300},
    "read_dht":  lambda text: {},
    "tft_show":  lambda text: {"text": text[:110]},
    "oled_show": lambda text: {"line1": text[:21]},   # 21 chars = one OLED line
}


def _llm_prompt(text: str, sensors: dict | None, voice: bool) -> str:
    """Prefix the user's text with live room context and, for voice, a brevity
    instruction. Routing and task logs still use the raw text."""
    parts = []
    if voice:
        parts.append(VOICE_STYLE)
    # Room questions only: on anything else llama3.2:3b remarks on the fact
    # ("not relevant to the information provided") instead of ignoring it.
    if _is_room_question(text) and sensors and sensors.get("temperature_c") is not None \
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
from backend.model_gateway.gateway import GatewayError, ModelGateway, ROLE_MODELS, WARM
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
    text: str = ""
    image: str | None = None   # base64 photo from the panel (upload / webcam)
    camera: bool = False       # take the photo with a device camera (the Pi)


class ActionRequest(BaseModel):
    action: str
    text: str = ""


# The web panel lives in panel.html next to this file (too big to inline).
_PANEL_HTML = (Path(__file__).with_name("panel.html")).read_text(encoding="utf-8")


def create_app(repo: Repository, gateway: ModelGateway,
               bus: "DeviceBus | None" = None,
               api_key: str | None = None,
               sensors: dict | None = None,
               event_log=None,
               images: dict | None = None) -> FastAPI:
    app = FastAPI(title="local-ai-ecosystem", version="1")
    registry = DeviceRegistry(repo)
    last: dict = {}        # most recent LLM question/answer, for the panel
    last_image: dict = {}  # the photo that answer was about, served by /last-image

    def require_key(x_api_key: str | None = Header(default=None)) -> None:
        # No key configured -> open (dev default). Configured -> must match.
        if api_key and x_api_key != api_key:
            raise HTTPException(status_code=401, detail="invalid or missing X-API-Key")

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> str:
        # Embed the key only for the laptop's own browser: the server binds
        # 0.0.0.0, so otherwise anyone on the LAN could read it from the page.
        # Other browsers ask for it once and keep it in localStorage.
        local = request.client is not None and request.client.host in ("127.0.0.1", "::1")
        return _PANEL_HTML.replace("__API_KEY__", (api_key or "") if local else "")

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
        if not _is_room_question(text):
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

    def _capture_image() -> str | None:
        """Ask a live camera device (the Pi) for a photo. The DeviceListener
        stores the result evt's image in `images`, as it does for readings.
        None if there's no camera or the capture failed."""
        if bus is None or images is None:
            return None
        for dev in registry.find_by_capability("capture_image"):
            did = dev["device_id"]
            before = images.get(did, {}).get("ts", 0)
            try:
                bus.send_command(build_command(did, "capture_image", {}, user="api"),
                                 collect_ms=CAMERA_WAIT_MS)
            except Exception as e:  # noqa: BLE001 - try the next camera
                log.warning("capture_image on %s failed: %s", did, e)
                continue
            for _ in range(20):   # listener may lag the bus by a few ms
                got = images.get(did)
                if got and got["ts"] != before:
                    return got["b64"]
                time.sleep(0.05)
        return None

    def _vision_input(decision, image: str | None, camera: bool,
                      auto_camera: bool) -> tuple[str, str | None]:
        """(role, image). An attached image always means vision. Otherwise a
        device camera is used when asked for (camera=True), or for a vision
        question when auto_camera is set (voice has no camera button)."""
        if image:
            return "vision", image
        if camera or (auto_camera and decision.role == "vision"):
            shot = _capture_image()
            if shot:
                return "vision", shot
            if camera:
                raise HTTPException(status_code=404,
                                    detail="no live camera - plug a USB webcam into the Pi")
        return _llm_role(decision.role), None

    def _answer(text: str, voice: bool = False, image: str | None = None,
                camera: bool = False) -> dict:
        text = text.strip() or (DESCRIBE if (image or camera) else text)
        decision = route(text)

        if decision.tier == "deterministic" and not (image or camera):
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
        role, img = _vision_input(decision, image, camera, auto_camera=voice)
        _refresh_room_reading(text)
        try:
            result = gateway.generate(role, _llm_prompt(text, sensors, voice),
                                      [img] if img else None)
        except GatewayError as e:
            log.warning("ask tier=llm role=%s FAILED: %s", role, e)
            raise HTTPException(status_code=502, detail=str(e))
        return _finish_llm(text, role, result, task_id, voice, img)

    def _finish_llm(text: str, role: str, result, task_id: str, voice: bool,
                    image: str | None = None) -> dict:
        """Shared tail of /ask, /listen and /ask/stream: log, record the task,
        mirror to the TFT, remember it for the panel, build the response."""
        m = result.metrics
        log.info(
            "ask tier=llm role=%s model=%s fell_back=%s total_ms=%s tok/s=%s",
            role, result.model, result.fell_back, m.total_ms, m.tokens_per_s,
        )
        repo.add_task(task_id, {"created_at": now_ms(), "text": text,
                                "role": role, "model": result.model})
        # Mirror the answer to any device that can show it (the Pi TFT). Reuses
        # the proven tft_show path; best-effort, never delays the HTTP answer.
        if bus is not None:
            threading.Thread(target=_mirror_to_tft, args=(result.text,),
                             daemon=True).start()
        last.update(text=text, answer=result.text, voice=voice,
                    model=result.model, ts=now_ms(), image=bool(image))
        last_image.clear()
        if image:
            last_image["b64"] = image
        return {
            "tier": "llm",
            "role": role,
            "model": result.model,
            "fell_back": result.fell_back,
            "answer": result.text,
            "metrics": m.__dict__,
            "task_id": task_id,
        }

    def _mood(devs: list[dict], now: int) -> str:
        """Same moods as the Pi TFT face, decided server-side for the panel."""
        if devs and not any(not d.get("stale") and d.get("online", True) for d in devs):
            return "offline"
        s = sensors or {}
        if s.get("temperature_c") is not None and s["temperature_c"] >= TEMP_ALERT_C \
                and now - s.get("ts", 0) < SENSOR_MAX_AGE_MS:
            return "alert"
        if last.get("ts") and now - last["ts"] < SPEAKING_MS:
            return "speaking"
        return "idle"

    @app.get("/status", dependencies=[Depends(require_key)])
    def status() -> dict:
        """Everything the panel shows, in one poll. Keyed: it includes the last
        question and answer."""
        now = now_ms()
        devs = registry.list(now=now)
        return {"ts": now, "mood": _mood(devs, now), "sensors": dict(sensors or {}),
                "devices": devs, "last": dict(last) or None,
                "log": list(event_log or [])[::-1]}

    @app.get("/last-image", dependencies=[Depends(require_key)])
    def last_image_file() -> Response:
        """The photo behind the last answer (panel thumbnail). Kept out of
        /status so the 2s poll doesn't resend it."""
        if not last_image:
            raise HTTPException(status_code=404, detail="no image")
        raw = base64.b64decode(last_image["b64"])
        kind = "image/png" if raw[:4] == b"\x89PNG" else "image/jpeg"
        return Response(raw, media_type=kind, headers={"Cache-Control": "no-store"})

    @app.post("/devices/{device_id}/action", dependencies=[Depends(require_key)])
    def device_action(device_id: str, req: ActionRequest) -> dict:
        """One panel button press: a whitelisted action on one live device."""
        if req.action not in _PANEL_ACTIONS:
            raise HTTPException(status_code=422,
                                detail=f"action {req.action!r} is not allowed from the panel")
        if bus is None:
            raise HTTPException(status_code=501, detail="no messaging layer configured")
        dev = next((d for d in registry.list() if d["device_id"] == device_id), None)
        if dev is None or dev.get("stale") or not dev.get("online", True):
            raise HTTPException(status_code=404, detail=f"{device_id} is not live")
        if req.action not in {c["name"] for c in dev.get("capabilities", [])}:
            raise HTTPException(status_code=422,
                                detail=f"{device_id} does not offer {req.action!r}")
        cmd = build_command(device_id, req.action, _PANEL_ACTIONS[req.action](req.text),
                            user="panel")
        states = [e.state.value for e in bus.send_command(cmd)]
        log.info("panel action=%s device=%s states=%s", req.action, device_id, states)
        return {"device_id": device_id, "action": req.action, "states": states}

    @app.post("/ask", dependencies=[Depends(require_key)])
    def ask(req: AskRequest) -> dict:
        return _answer(req.text, image=_clean_image(req.image), camera=req.camera)

    @app.post("/ask/stream", dependencies=[Depends(require_key)])
    def ask_stream(req: AskRequest) -> StreamingResponse:
        """Same routing as /ask, but LLM answers arrive piece by piece as
        server-sent events: {"t":"chunk","text":...} then {"t":"done",...}.
        Deterministic actions and errors arrive as one event."""
        def sse(obj: dict) -> str:
            return f"data: {json.dumps(obj)}\n\n"

        image = _clean_image(req.image)   # reject bad uploads before streaming starts
        text = req.text.strip() or (DESCRIBE if (image or req.camera) else req.text)

        def events():
            decision = route(text)
            if decision.tier == "deterministic" and not (image or req.camera):
                try:
                    yield sse({"t": "done", **_answer(text)})
                except HTTPException as e:
                    yield sse({"t": "error", "detail": e.detail})
                return
            task_id = uuid.uuid4().hex
            if req.camera and not image:
                yield sse({"t": "status", "text": "taking a photo with the Pi camera..."})
            try:
                role, img = _vision_input(decision, image, req.camera, auto_camera=False)
            except HTTPException as e:
                yield sse({"t": "error", "detail": e.detail})
                return
            # Tell the panel which model is about to run: a specialist swap
            # takes 10-40s, which otherwise looks like a hang.
            model = gateway.first_model(role)
            yield sse({"t": "route", "role": role, "model": model, "swap": model != WARM})
            _refresh_room_reading(text)
            gen = gateway.stream(role, _llm_prompt(text, sensors, False),
                                 [img] if img else None)
            try:
                while True:
                    yield sse({"t": "chunk", "text": next(gen)})
            except StopIteration as stop:
                yield sse({"t": "done", **_finish_llm(text, role, stop.value,
                                                      task_id, False, img)})
            except GatewayError as e:
                log.warning("ask/stream role=%s FAILED: %s", role, e)
                yield sse({"t": "error", "detail": str(e)})

        # Sync generator: Starlette iterates it in a worker thread, so the
        # blocking Ollama stream doesn't stall the event loop.
        return StreamingResponse(events(), media_type="text/event-stream")

    def _transcribe(audio: UploadFile) -> str:
        """faster-whisper on an uploaded clip: the Pi's WAV or the browser's
        webm/ogg (PyAV decodes both). Capped so one upload can't eat the disk."""
        data = audio.file.read(MAX_AUDIO_BYTES + 1)
        if len(data) > MAX_AUDIO_BYTES:
            raise HTTPException(status_code=413, detail="audio too long")
        suffix = os.path.splitext(audio.filename or "")[1][:8] or ".wav"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
            f.write(data)
            path = f.name
        try:
            segments, _ = _get_stt().transcribe(path, beam_size=1)
            text = " ".join(s.text for s in segments).strip()
        finally:
            os.unlink(path)
        log.info("transcribed %d bytes (%s) -> %r", len(data), suffix, text)
        return text

    @app.post("/transcribe", dependencies=[Depends(require_key)])
    def transcribe(audio: UploadFile = File(...)) -> dict:
        """Speech-to-text only, for the panel's mic button: the text then goes
        through /ask/stream like typed text (so it can carry a photo too)."""
        return {"transcript": _transcribe(audio)}

    @app.post("/listen", dependencies=[Depends(require_key)])
    def listen(audio: UploadFile = File(...)) -> dict:
        """Voice entry: the Pi uploads a short WAV, we transcribe it with
        faster-whisper and run it through the same /ask logic (so the TFT mirror
        fires too). Sync def on purpose: FastAPI runs it in a worker thread, so
        slow STT + LLM don't freeze /health, /devices and the web UI."""
        text = _transcribe(audio)
        if not text:
            return {"transcript": "", "answer": None}
        result = _answer(text, voice=True)
        result["transcript"] = text
        return result

    return app


def _build_default_app() -> FastAPI:
    """Wire real SQLite + Ollama for uvicorn. Not used by tests."""
    # uvicorn only configures its own loggers; without this every eco.api
    # log.info (asks, transcripts, panel actions) was silently dropped.
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
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
    event_log = collections.deque(maxlen=40)   # filled by the listener, shown on the panel
    images: dict = {}    # latest camera photo per device, filled by the listener
    app = create_app(SQLiteRepository(db), gateway, bus=bus,
                     api_key=os.environ.get("ECO_API_KEY"), sensors=sensors,
                     event_log=event_log, images=images)
    # Background listener auto-registers devices and keeps them live from their
    # retained manifest + periodic state, so the web UI shows real status.
    listener = DeviceListener(DeviceRegistry(SQLiteRepository(db)), broker, port,
                              mq_user, mq_pass, sensors=sensors, log=event_log,
                              images=images)
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
