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
from backend.model_gateway.gateway import GatewayError, ModelGateway, ROLE_MODELS
from backend.orchestrator.router import route
from backend.storage.sqlite_repo import Repository, SQLiteRepository
from schemas.messages import Manifest, now_ms

log = logging.getLogger("eco.api")

# Actions that cannot be fulfilled until the Phase-2 messaging layer exists.
_NEEDS_MESSAGING = {"estop": "e-stop", "capture_image": "image capture",
                    "device_status": "device status"}


class AskRequest(BaseModel):
    text: str


def create_app(repo: Repository, gateway: ModelGateway) -> FastAPI:
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
            # estop / capture / device_status need the messaging layer
            label = _NEEDS_MESSAGING.get(action, action)
            log.info("ask tier=deterministic action=%s -> phase2", action)
            raise HTTPException(
                status_code=501,
                detail=f"{label} requires messaging layer (Phase 2)",
            )

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
    client = httpx.Client(base_url=f"http://{host}")
    # Image support on the text primary is decided by verify_models.ps1; default
    # to False so vision routes to the real vision model unless proven otherwise.
    supports = os.environ.get("VISION_PRIMARY_IMAGES", "0") == "1"
    gateway = ModelGateway(client, vision_primary_supports_images=supports)
    return create_app(SQLiteRepository(db), gateway)


app = _build_default_app()
