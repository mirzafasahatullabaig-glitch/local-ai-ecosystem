import httpx
import pytest

from backend.model_gateway.gateway import (
    GatewayError,
    ModelGateway,
    PRIMARY,
)

# A realistic Ollama /api/generate body (durations in nanoseconds).
OK_BODY = {
    "response": "hello",
    "total_duration": 2_000_000_000,       # 2000 ms
    "load_duration": 500_000_000,          # 500 ms
    "prompt_eval_duration": 300_000_000,   # 300 ms
    "eval_duration": 1_000_000_000,        # 1000 ms
    "eval_count": 50,                      # -> 50 tokens / 1.0s = 50 tok/s
}


def make_gateway(handler, **kw):
    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport, base_url="http://127.0.0.1:11434")
    return ModelGateway(client, **kw)


def test_primary_path_and_metrics():
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req.url.path)
        return httpx.Response(200, json=OK_BODY)

    gw = make_gateway(handler)
    res = gw.generate("reason", "hi")
    assert res.model == PRIMARY
    assert res.fell_back is False
    assert res.text == "hello"
    # metrics math
    m = res.metrics
    assert m.total_ms == 2000.0
    assert m.load_duration_ms == 500.0
    assert m.prompt_eval_ms == 300.0
    assert m.eval_ms == 1000.0
    assert m.tokens == 50
    assert m.tokens_per_s == 50.0
    # primary does not unload anything (no /api/ps dance)
    assert "/api/ps" not in calls


def test_fallback_on_primary_500():
    def handler(req: httpx.Request) -> httpx.Response:
        body = req.content.decode()
        # primary fails; fallback (llama3.2:3b) succeeds
        if f'"{PRIMARY}"' in body and '"prompt"' in body:
            return httpx.Response(500, json={"error": "boom"})
        return httpx.Response(200, json=OK_BODY)

    gw = make_gateway(handler)
    res = gw.generate("reason", "hi")
    assert res.model == "llama3.2:3b"
    assert res.fell_back is True
    assert res.tried == [PRIMARY, "llama3.2:3b"]


def test_all_models_fail_raises():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "down"})

    gw = make_gateway(handler)
    with pytest.raises(GatewayError):
        gw.generate("reason", "hi")


def test_specialist_unloads_before_loading():
    """A code request must evict the warm primary before the specialist runs."""
    order = []

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        body = req.content.decode()
        if path == "/api/ps":
            order.append("ps")
            return httpx.Response(200, json={"models": [{"name": PRIMARY}]})
        if '"keep_alive":0' in body.replace(" ", ""):
            order.append(f"unload:{_model(body)}")
            return httpx.Response(200, json={})
        if '"prompt"' in body:
            order.append(f"generate:{_model(body)}")
            return httpx.Response(200, json=OK_BODY)
        # warm primary (keep_alive -1, no prompt)
        order.append(f"warm:{_model(body)}")
        return httpx.Response(200, json={})

    gw = make_gateway(handler)
    res = gw.generate("code", "write a function")
    assert res.model == "qwen2.5-coder:7b"
    # ps checked, primary unloaded, THEN specialist generates
    assert order.index("ps") < order.index("unload:qwen3.5:4b")
    assert order.index("unload:qwen3.5:4b") < order.index("generate:qwen2.5-coder:7b")
    # primary re-warmed after
    assert order[-1] == f"warm:{PRIMARY}"


def test_vision_routes_to_vl_when_primary_cant_see():
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/ps":
            return httpx.Response(200, json={"models": []})
        return httpx.Response(200, json=OK_BODY)

    gw = make_gateway(handler, vision_primary_supports_images=False)
    res = gw.generate("vision", "what do you see")
    assert res.model == "qwen3-vl:2b"


def _model(body: str) -> str:
    import json
    return json.loads(body)["model"]
