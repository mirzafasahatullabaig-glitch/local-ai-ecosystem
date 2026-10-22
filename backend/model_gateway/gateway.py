"""Talks to Ollama. Picks a model per role, enforces one-model-in-VRAM, falls
back on failure, and records timing metrics for every call.

VRAM rule: only the primary (qwen3.5:4b) stays warm (keep_alive=-1). Before a
specialist runs we unload everything else (keep_alive=0), run the specialist
with a short keep_alive, then re-warm the primary. The registry and router are
not touched here.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional

import httpx

PRIMARY = "qwen3.5:4b"          # the reasoning model (vision/plan/reason-fallback)
WARM = "llama3.2:3b"            # kept resident in VRAM; fast, no reasoning trace
PRIMARY_NUM_CTX = 4096

# role -> (primary, fallback). WARM (llama3.2:3b) is the resident model, so
# reason — the common interactive path — answers in a few seconds with no
# reload. qwen3.5:4b handles vision/plan and backs up reason; it is loaded on
# demand (unload WARM, run, re-warm WARM). plan keeps qwen first since
# multi-step tasks benefit from the reasoning trace.
ROLE_MODELS: dict[str, tuple[str, str]] = {
    "reason": (WARM, PRIMARY),
    "vision": (PRIMARY, "qwen3-vl:2b"),
    "code": ("qwen2.5-coder:7b", "deepseek-coder:6.7b"),
    "plan": (PRIMARY, WARM),
}


@dataclass
class CallMetrics:
    model: str
    load_duration_ms: float
    prompt_eval_ms: float
    eval_ms: float
    total_ms: float
    tokens: int
    tokens_per_s: float


@dataclass
class GatewayResult:
    text: str
    model: str
    metrics: CallMetrics
    fell_back: bool = False
    tried: list[str] = field(default_factory=list)


class GatewayError(RuntimeError):
    """All candidate models failed."""


def _ns_to_ms(ns: Optional[int]) -> float:
    return round((ns or 0) / 1e6, 2)


def _metrics_from_response(model: str, body: dict) -> CallMetrics:
    eval_count = body.get("eval_count", 0) or 0
    eval_ns = body.get("eval_duration", 0) or 0
    total_ms = _ns_to_ms(body.get("total_duration"))
    tps = round(eval_count / (eval_ns / 1e9), 2) if eval_ns else 0.0
    return CallMetrics(
        model=model,
        load_duration_ms=_ns_to_ms(body.get("load_duration")),
        prompt_eval_ms=_ns_to_ms(body.get("prompt_eval_duration")),
        eval_ms=_ns_to_ms(eval_ns),
        total_ms=total_ms,
        tokens=eval_count,
        tokens_per_s=tps,
    )


class ModelGateway:
    def __init__(
        self,
        client: httpx.Client,
        timeout_s: float = 120.0,
        vision_primary_supports_images: bool = False,
    ) -> None:
        self._client = client
        self._timeout = timeout_s
        self._vision_primary_ok = vision_primary_supports_images

    # --- VRAM management --------------------------------------------------

    def _loaded_models(self) -> list[str]:
        r = self._client.get("/api/ps", timeout=self._timeout)
        r.raise_for_status()
        return [m["name"] for m in r.json().get("models", [])]

    def _unload(self, model: str) -> None:
        # keep_alive 0 tells Ollama to evict the model immediately.
        self._client.post(
            "/api/generate",
            json={"model": model, "keep_alive": 0},
            timeout=self._timeout,
        )

    def _warm_primary(self) -> None:
        # Keep the WARM model resident so the common reason path never reloads.
        self._client.post(
            "/api/generate",
            json={"model": WARM, "keep_alive": -1,
                  "options": {"num_ctx": PRIMARY_NUM_CTX}},
            timeout=self._timeout,
        )

    def _ensure_only(self, model: str) -> None:
        """Evict every loaded model that is not ``model``."""
        for loaded in self._loaded_models():
            if loaded != model:
                self._unload(loaded)

    # --- generation -------------------------------------------------------

    def _generate(self, model: str, prompt: str, keep_alive, options: dict,
                  images: list[str] | None = None) -> dict:
        body = {"model": model, "prompt": prompt, "stream": False,
                "keep_alive": keep_alive, "options": options}
        if images:
            body["images"] = images   # base64, no data: prefix (Ollama's format)
        r = self._client.post(
            "/api/generate",
            json=body,
            timeout=self._timeout,
        )
        r.raise_for_status()
        return r.json()

    def _prepare(self, model: str) -> tuple[dict, object]:
        """(options, keep_alive) for a call; evicts other models for specialists."""
        if model == WARM:
            return {"num_ctx": PRIMARY_NUM_CTX}, -1
        # specialist: clear VRAM first so two models never co-reside
        self._ensure_only(model)
        return {}, "2m"

    def _call_one(self, model: str, prompt: str,
                  images: list[str] | None = None) -> GatewayResult:
        options, keep_alive = self._prepare(model)

        body = self._generate(model, prompt, keep_alive, options, images)

        if model != WARM:
            self._warm_primary()  # restore the warm default

        return GatewayResult(
            text=body.get("response", ""),
            model=model,
            metrics=_metrics_from_response(model, body),
        )

    def _models_for(self, role: str) -> list[str]:
        primary, fallback = ROLE_MODELS[role]
        if role == "vision" and not self._vision_primary_ok:
            # text primary can't take images -> go straight to the vision model
            return [fallback]
        return [primary, fallback]

    def first_model(self, role: str) -> str:
        """The model a request for `role` will try first (for the panel)."""
        return self._models_for(role)[0]

    def generate(self, role: str, prompt: str,
                 images: list[str] | None = None) -> GatewayResult:
        """Run the request through the role's models, falling back on failure."""
        if role not in ROLE_MODELS:
            raise GatewayError(f"unknown role: {role}")

        candidates = self._models_for(role)
        tried: list[str] = []
        last_err: Optional[Exception] = None

        for i, model in enumerate(candidates):
            tried.append(model)
            try:
                result = self._call_one(model, prompt, images)
                result.fell_back = i > 0
                result.tried = tried
                return result
            except (httpx.HTTPError, httpx.TimeoutException) as e:
                last_err = e
                continue

        raise GatewayError(
            f"all models failed for role {role}: tried {tried} ({last_err})"
        )

    def stream(self, role: str, prompt: str, images: list[str] | None = None):
        """Like generate(), but a generator: yields text pieces as Ollama makes
        them and returns the final GatewayResult (StopIteration.value). Falls
        back only if a model fails before its first piece — a half-streamed
        answer can't be retried cleanly."""
        if role not in ROLE_MODELS:
            raise GatewayError(f"unknown role: {role}")

        tried: list[str] = []
        last_err: Optional[Exception] = None
        for i, model in enumerate(self._models_for(role)):
            tried.append(model)
            parts: list[str] = []
            try:
                options, keep_alive = self._prepare(model)
                final: dict = {}
                body = {"model": model, "prompt": prompt, "stream": True,
                        "keep_alive": keep_alive, "options": options}
                if images:
                    body["images"] = images
                with self._client.stream(
                    "POST", "/api/generate",
                    json=body,
                    timeout=self._timeout,
                ) as r:
                    r.raise_for_status()
                    for line in r.iter_lines():
                        if not line:
                            continue
                        body = json.loads(line)
                        piece = body.get("response", "")
                        if piece:
                            parts.append(piece)
                            yield piece
                        if body.get("done"):
                            final = body
                if model != WARM:
                    self._warm_primary()
                return GatewayResult(text="".join(parts), model=model,
                                     metrics=_metrics_from_response(model, final),
                                     fell_back=i > 0, tried=tried)
            except (httpx.HTTPError, httpx.TimeoutException) as e:
                if parts:
                    raise GatewayError(f"{model} failed mid-answer: {e}") from e
                last_err = e

        raise GatewayError(
            f"all models failed for role {role}: tried {tried} ({last_err})"
        )
