"""Developer page (/dev) and its endpoints: LLM traces, raw MQTT traffic, a
raw command sender, logs and system health. Everything here is behind the API
key; the page itself embeds the key only for the laptop's own browser.

Kept out of main.py so the user-facing API stays readable. The Pi is reached
over ssh with key auth (the station runs as the user who owns that key).
"""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from backend.messaging.bus import build_command
from schemas.messages import now_ms

_DEV_HTML = (Path(__file__).with_name("dev.html")).read_text(encoding="utf-8")
PI_SSH = os.environ.get("ECO_PI_SSH", "")   # user@pi-address, set in .env.mqtt
LOG_FILE = os.environ.get("ECO_LOG_FILE", "api.log")   # start-station.bat appends here
# source name -> systemd unit on the Pi (None = the station's own api.log).
# Fixed map: the query string only ever picks a key, never builds a command.
LOG_SOURCES = {"api": None, "agent": "eco-agent", "tft": "eco-tft",
               "touch": "eco-touch", "broker": "mosquitto"}
MAX_PARAMS_BYTES = 2000

# For the panel and this page (not /docs, which loads Swagger from a CDN).
# connect-src 'self' is the point: the key sits in localStorage on non-laptop
# browsers, so even if an XSS slipped through, the browser refuses to send it
# anywhere but this station. Inline script/style is how both pages are built.
PAGE_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; "
        "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


class DevCommand(BaseModel):
    device_id: str
    action: str
    params: dict = {}


def _ssh(cmd: str, timeout: float = 8) -> str | None:
    """Run a fixed command on the Pi; None if it's unreachable (or not configured)."""
    if not PI_SSH:
        return None
    try:
        out = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=4", PI_SSH, cmd],
                             capture_output=True, text=True, timeout=timeout)
        return out.stdout if out.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def _tail(path: str, lines: int) -> list[str]:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 400_000))   # plenty for 500 lines, never the whole file
            return f.read().decode("utf-8", "replace").splitlines()[-lines:]
    except OSError as e:
        return [f"(cannot read {path}: {e})"]


# --- laptop CPU / RAM without psutil (the station is Windows) ---------------
# shortcut: Windows-only via kernel32; returns None elsewhere. Add psutil if the
# station ever moves to Linux.
class _MemStatus(ctypes.Structure):
    _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong)] + \
               [(n, ctypes.c_ulonglong) for n in ("total", "avail", "tpf", "apf", "tv", "av", "aev")]


_last_cpu: list[int] | None = None


def _ram_mb() -> tuple[int, int] | None:
    if os.name != "nt":
        return None
    m = _MemStatus()
    m.dwLength = ctypes.sizeof(m)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
        return None
    return (m.total - m.avail) // 2**20, m.total // 2**20


def _cpu_percent() -> float | None:
    """Busy % since the previous call (the page polls, so that's the window)."""
    global _last_cpu
    if os.name != "nt":
        return None
    idle, kern, user = (ctypes.c_ulonglong() for _ in range(3))
    if not ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kern),
                                                 ctypes.byref(user)):
        return None
    now = [idle.value, kern.value, user.value]   # kernel time includes idle
    prev, _last_cpu = _last_cpu, now
    if prev is None:
        return None
    d_idle, d_total = now[0] - prev[0], (now[1] - prev[1]) + (now[2] - prev[2])
    return round(100 * (d_total - d_idle) / d_total, 1) if d_total else None


def _gpu() -> dict | None:
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.used,memory.total,"
                              "utilization.gpu,temperature.gpu", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=5).stdout.strip()
        name, used, total, util, temp = [x.strip() for x in out.splitlines()[0].split(",")]
        return {"name": name, "mem_used_mb": int(used), "mem_total_mb": int(total),
                "util_pct": int(util), "temp_c": int(temp)}
    except (OSError, subprocess.TimeoutExpired, ValueError, IndexError):
        return None


_PI_HEALTH_CMD = (
    "for s in eco-agent eco-tft eco-touch mosquitto; do echo svc $s $(systemctl is-active $s); done; "
    "echo temp $(cat /sys/class/thermal/thermal_zone0/temp); "
    "echo load $(cut -d' ' -f1 /proc/loadavg); "
    "free -m | awk '/Mem:/{print \"mem\", $3, $2}'; "
    "echo up $(cut -d. -f1 /proc/uptime)"
)


def _pi_health() -> dict | None:
    out = _ssh(_PI_HEALTH_CMD)
    if out is None:
        return None
    h: dict = {"services": {}}
    for line in out.splitlines():
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "svc" and len(parts) == 3:
            h["services"][parts[1]] = parts[2]
        elif parts[0] == "temp" and len(parts) == 2 and parts[1].isdigit():
            h["temp_c"] = round(int(parts[1]) / 1000, 1)
        elif parts[0] == "load" and len(parts) == 2:
            h["load_1m"] = float(parts[1])
        elif parts[0] == "mem" and len(parts) == 3:
            h["mem_used_mb"], h["mem_total_mb"] = int(parts[1]), int(parts[2])
        elif parts[0] == "up" and len(parts) == 2:
            h["uptime_s"] = int(parts[1])
    return h


def add_dev_routes(app: FastAPI, *, require_key, registry, bus, gateway, api_key,
                   traffic, traces) -> None:
    keyed = [Depends(require_key)]

    @app.get("/dev", response_class=HTMLResponse, include_in_schema=False)
    def dev_page(request: Request) -> HTMLResponse:
        # Same rule as the panel: the key is embedded only for the laptop itself.
        local = request.client is not None and request.client.host in ("127.0.0.1", "::1")
        return HTMLResponse(_DEV_HTML.replace("__API_KEY__", (api_key or "") if local else ""),
                            headers=PAGE_HEADERS)

    @app.get("/dev/state", dependencies=keyed, tags=["developer"])
    def dev_state(since: int = 0) -> dict:
        """LLM traces (newest first), MQTT traffic after `since`, raw device records."""
        return {"ts": now_ms(),
                "traces": list(traces or [])[::-1],
                "traffic": [t for t in (traffic or []) if t["seq"] > since],
                "devices": registry.list()}

    @app.get("/dev/models", dependencies=keyed, tags=["developer"])
    def dev_models() -> dict:
        """What Ollama has loaded right now, with GPU memory use."""
        try:
            return {"models": gateway.loaded_details()}
        except Exception as e:  # noqa: BLE001 - Ollama down is a state, not a crash
            return {"models": [], "error": str(e)}

    @app.post("/dev/command", dependencies=keyed, tags=["developer"])
    def dev_command(cmd: DevCommand) -> dict:
        """Send one raw command. Only actions the device itself advertises in
        its manifest; params are passed through as-is (2 KB cap)."""
        if bus is None:
            raise HTTPException(status_code=501, detail="no messaging layer configured")
        dev = next((d for d in registry.list() if d["device_id"] == cmd.device_id), None)
        if dev is None or dev.get("stale") or not dev.get("online", True):
            raise HTTPException(status_code=404, detail=f"{cmd.device_id} is not live")
        if cmd.action not in {c["name"] for c in dev.get("capabilities", [])}:
            raise HTTPException(status_code=422,
                                detail=f"{cmd.device_id} does not advertise {cmd.action!r}")
        if len(json.dumps(cmd.params)) > MAX_PARAMS_BYTES:
            raise HTTPException(status_code=413, detail="params too large")
        command = build_command(cmd.device_id, cmd.action, cmd.params, user="dev")
        events = bus.send_command(command, collect_ms=5000)
        return {"command": json.loads(command.model_dump_json()),
                "states": [e.state.value for e in events]}

    @app.get("/dev/logs", dependencies=keyed, tags=["developer"])
    def dev_logs(source: str = "api", lines: int = Query(150, ge=10, le=500)) -> dict:
        """Tail of the station's api.log or a Pi service's journal."""
        if source not in LOG_SOURCES:
            raise HTTPException(status_code=422, detail=f"source must be one of {list(LOG_SOURCES)}")
        unit = LOG_SOURCES[source]
        if unit is None:
            return {"source": source, "lines": _tail(LOG_FILE, lines)}
        out = _ssh(f"journalctl -u {unit} -n {lines} --no-pager -o short-iso")
        return {"source": source,
                "lines": out.splitlines() if out is not None else ["(Pi unreachable over ssh)"]}

    @app.get("/dev/health", dependencies=keyed, tags=["developer"])
    def dev_health() -> dict:
        """Laptop CPU/RAM/GPU, Ollama, and the Pi's services, temperature and load."""
        ram = _ram_mb()
        try:
            ollama = {"up": True, "loaded": gateway.loaded_details()}
        except Exception as e:  # noqa: BLE001
            ollama = {"up": False, "error": str(e)}
        return {"ts": now_ms(),
                "laptop": {"cpu_pct": _cpu_percent(),
                           "ram_used_mb": ram[0] if ram else None,
                           "ram_total_mb": ram[1] if ram else None,
                           "gpu": _gpu()},
                "ollama": ollama,
                "pi": _pi_health()}
