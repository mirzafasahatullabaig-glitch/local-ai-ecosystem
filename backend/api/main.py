"""HTTP surface. Built via create_app(repo, gateway) so tests inject fakes.

The router decides tier/role; deterministic read-only actions are answered
here, but anything needing a device (estop, capture) returns an explicit
Phase-2 error instead of a fake success. Every /ask is logged with tier,
model and metrics.
"""
from __future__ import annotations

import logging
import os
import time
import uuid

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

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


def create_app(repo: Repository, gateway: ModelGateway,
               bus: "DeviceBus | None" = None) -> FastAPI:
    app = FastAPI(title="local-ai-ecosystem", version="1")
    registry = DeviceRegistry(repo)

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "ts": now_ms()}

    @app.get("/models")
    def models() -> dict:
        return {"roles": {r: list(m) for r, m in ROLE_MODELS.items()}}

    @app.get("/devices")
    def devices() -> dict:
        return {"devices": registry.list()}

    @app.post("/devices/register")
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

    @app.post("/ask")
    def ask(req: AskRequest) -> dict:
        decision = route(req.text)

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
        try:
            result = gateway.generate(decision.role, req.text)
        except GatewayError as e:
            log.warning("ask tier=llm role=%s FAILED: %s", decision.role, e)
            raise HTTPException(status_code=502, detail=str(e))

        m = result.metrics
        log.info(
            "ask tier=llm role=%s model=%s fell_back=%s total_ms=%s tok/s=%s",
            decision.role, result.model, result.fell_back, m.total_ms, m.tokens_per_s,
        )
        repo.add_task(task_id, {"created_at": now_ms(), "text": req.text,
                                "role": decision.role, "model": result.model})
        return {
            "tier": "llm",
            "role": decision.role,
            "model": result.model,
            "fell_back": result.fell_back,
            "answer": result.text,
            "metrics": m.__dict__,
            "task_id": task_id,
        }

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
    from backend.messaging.bus import MqttDeviceBus
    bus = MqttDeviceBus(broker, int(os.environ.get("ECO_BROKER_PORT", "1883")))
    return create_app(SQLiteRepository(db), gateway, bus=bus)


app = _build_default_app()
