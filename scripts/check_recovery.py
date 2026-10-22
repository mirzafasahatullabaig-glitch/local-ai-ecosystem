"""Failure/recovery check against the LIVE system: no mocks.

WARNING: this deliberately disrupts things. It kills the Pi agent (systemd
restarts it), sends malformed / unknown / expired / duplicate commands to the
Pi and the ESP (harmless: ping only), and restarts the station.

Run from the repo root on the laptop:  .venv\Scripts\python scripts\check_recovery.py
Needs: .env.mqtt (incl. ECO_BROKER, ECO_PI_SSH), the station running, key-based ssh to the Pi.
"""
import json, pathlib, subprocess, sys, threading, time, uuid
import httpx
import paho.mqtt.client as mqtt

sys.stdout.reconfigure(encoding="utf-8")
ENV = dict(l.strip().split("=", 1) for l in open(pathlib.Path(__file__).resolve().parents[1] / ".env.mqtt") if "=" in l)
H = {"X-API-Key": ENV["ECO_API_KEY"]}
S = "http://127.0.0.1:8000"
BROKER = ENV["ECO_BROKER"]

# --- one MQTT client that records every evt ---
events = []          # (t, topic, payload dict)
lock = threading.Lock()
cli = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"recovery-test-{uuid.uuid4().hex[:6]}")
cli.username_pw_set(ENV["ECO_MQTT_USER"], ENV["ECO_MQTT_PASS"])
cli.on_connect = lambda c, u, f, rc, p=None: c.subscribe("eco/v1/devices/+/evt", qos=1)
def on_msg(c, u, m):
    try:
        body = json.loads(m.payload)
    except ValueError:
        return
    with lock:
        events.append((time.time(), m.topic, body))
cli.on_message = on_msg
cli.connect(BROKER, 1883, 30); cli.loop_start(); time.sleep(1)


def now_ms():
    return int(time.time() * 1000)


def command(dev, action, params=None, cid=None, expires_in_ms=30_000):
    t = now_ms()
    if expires_in_ms < 0:          # a realistic stale command: issued 90s ago, expired 60s ago
        t, expires_in_ms = t - 90_000, 30_000
    return {"schema_version": 1, "msg_id": uuid.uuid4().hex, "device_id": dev, "ts": t,
            "command_id": cid or uuid.uuid4().hex, "action": action, "params": params or {},
            "issued_at": t, "expires_at": t + expires_in_ms, "seq": 0, "origin": {"user": "recovery-test"}}


def send(dev, payload, wait=3.0):
    """Publish raw payload; return the states seen for its command_id (if any)."""
    cid = payload.get("command_id") if isinstance(payload, dict) else None
    start = time.time()
    cli.publish(f"eco/v1/devices/{dev}/cmd", json.dumps(payload) if isinstance(payload, dict) else payload, qos=1)
    time.sleep(wait)
    with lock:
        return [e[2].get("state") for e in events if e[0] >= start and cid and e[2].get("command_id") == cid]


def live(dev):
    for d in httpx.get(f"{S}/status", headers=H, timeout=10).json()["devices"]:
        if d["device_id"] == dev:
            return bool(d.get("online", True)) and not d.get("stale")
    return False


def pi(cmd):
    return subprocess.run(["ssh", ENV["ECO_PI_SSH"], cmd], capture_output=True, text=True, timeout=30).stdout.strip()


results = []
def report(name, ok, evidence):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name:42} {evidence}")


# 1. Pi agent crash (SIGKILL: no clean disconnect, so the broker's last-will must fire)
assert live("raspberrypi"), "Pi not live before test"
pid_before = pi("systemctl show eco-agent -p MainPID --value")
t0 = time.time()
pi("kill -9 $(systemctl show eco-agent -p MainPID --value)")
t_off = t_on = None
while time.time() - t0 < 40:
    if t_off is None and not live("raspberrypi"):
        t_off = time.time() - t0
    if t_off is not None and live("raspberrypi"):
        t_on = time.time() - t0
        break
    time.sleep(0.3)
pid_after = pi("systemctl show eco-agent -p MainPID --value")
report("Pi agent crash: marked offline", t_off is not None, f"after {t_off:.1f}s" if t_off else "never")
report("Pi agent crash: auto-restart + back online", t_on is not None and pid_after != pid_before,
       f"online again after {t_on:.1f}s, pid {pid_before} -> {pid_after}" if t_on else "not back in 40s")
states = send("raspberrypi", command("raspberrypi", "ping"))
report("Pi agent after restart: commands work", states[-1:] == ["completed"], " > ".join(states))

# 2. malformed commands: device must reject and keep working
for dev in ("raspberrypi", "esp32_dev_01"):
    send(dev, "this is not json", wait=1)
    send(dev, {"action": "beep"}, wait=1)                       # missing every required field
    send(dev, {**command(dev, "ping"), "command_id": ""}, wait=1)   # empty id
    states = send(dev, command(dev, "ping"))
    report(f"{dev}: survives malformed commands", states[-1:] == ["completed"],
           f"valid ping afterwards: {' > '.join(states)}")

# 3. unknown action is refused, not faked
for dev in ("raspberrypi", "esp32_dev_01"):
    states = send(dev, command(dev, "self_destruct"))
    report(f"{dev}: unknown action refused", states[-1:] in (["failed"], ["rejected"]),
           " > ".join(states))

# 4. expired command must not run
for dev in ("raspberrypi", "esp32_dev_01"):
    states = send(dev, command(dev, "ping", expires_in_ms=-60_000))
    report(f"{dev}: expired command not executed", "completed" not in states and "expired" in states,
           " > ".join(states) or "(no events)")

# 5. duplicate delivery: same command_id twice must run once
for dev in ("raspberrypi", "esp32_dev_01"):
    cid = uuid.uuid4().hex
    first = send(dev, command(dev, "ping", cid=cid), wait=2)
    second = send(dev, command(dev, "ping", cid=cid), wait=2)
    report(f"{dev}: duplicate command runs once", "completed" not in second,
           f"1st: {' > '.join(first)} | 2nd: {' > '.join(second) or '(ignored)'}")

# 6. station restart: devices come back without anyone touching them
t0 = time.time()
subprocess.run(["powershell", "-NoProfile", "-Command",
  "$p=(Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue).OwningProcess;"
  "if($p){$pp=(Get-CimInstance Win32_Process -Filter \"ProcessId=$p\").ParentProcessId; Stop-Process -Id $p,$pp -Force -ErrorAction SilentlyContinue};"
  f"Start-Sleep 2; Start-Process -FilePath '{pathlib.Path(__file__).resolve().parent / 'start-station.bat'}' "
  "-WindowStyle Minimized"])
both = None
while time.time() - t0 < 60:
    try:
        if live("raspberrypi") and live("esp32_dev_01"):
            both = time.time() - t0
            break
    except httpx.HTTPError:
        pass
    time.sleep(0.5)
report("station restart: both devices live again", both is not None,
       f"{both:.1f}s after kill (incl. 2s wait)" if both else "not within 60s")
r = httpx.post(f"{S}/ask", headers=H, json={"text": "what is 3 plus 3"}, timeout=60)
report("station restart: LLM answers", r.status_code == 200, repr(r.json().get("answer", "")[:30]))

cli.loop_stop(); cli.disconnect()
print(f"\n{sum(results)}/{len(results)} passed")
