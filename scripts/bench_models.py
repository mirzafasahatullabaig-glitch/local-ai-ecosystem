"""Measures real Ollama latency: cold vs warm, a vision call, and 1 vs 2
concurrent requests. Writes median + p95 to bench_results/*.csv, and records
`ollama ps` so we can see whether a model sits 100% on GPU or spills to CPU.

Run inside the venv. This makes REAL model calls; it is never run in pytest.
Usage: python scripts/bench_models.py [--runs N]
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import csv
import os
import re
import statistics
import subprocess
import sys
import time
from pathlib import Path

import httpx

OLLAMA = os.environ.get("OLLAMA_HOST", "127.0.0.1:11434")
OLLAMA = re.sub(r"^https?://", "", OLLAMA)
BASE = f"http://{OLLAMA}"
PRIMARY = "qwen3.5:4b"
VISION = "qwen3-vl:2b"
OUT = Path("bench_results")

def _red_png_b64() -> str:
    """A 32x32 solid-red PNG, base64. Built with zlib+struct, no Pillow dep."""
    import base64
    import struct
    import zlib

    w = h = 32
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * w for _ in range(h))  # filter byte + RGB

    def chunk(tag: bytes, data: bytes) -> bytes:
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)  # 8-bit, truecolor
    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", ihdr)
           + chunk(b"IDAT", zlib.compress(raw))
           + chunk(b"IEND", b""))
    return base64.b64encode(png).decode()


VISION_PNG = _red_png_b64()


def free_gpu() -> None:
    """Run the PowerShell gate before a cold run. Abort the bench if it fails."""
    script = Path(__file__).with_name("free_gpu.ps1")
    r = subprocess.run(
        ["powershell", "-ExecutionPolicy", "Bypass", "-File", str(script)],
        capture_output=True, text=True,
    )
    print(r.stdout.strip())
    if r.returncode != 0:
        print(r.stderr.strip(), file=sys.stderr)
        sys.exit("free_gpu failed; aborting benchmark")


def generate(client: httpx.Client, model: str, prompt: str,
             keep_alive="2m", images=None) -> float:
    """One call; returns total_duration in ms from Ollama's own metrics."""
    body = {"model": model, "prompt": prompt, "stream": False,
            "keep_alive": keep_alive}
    if images:
        body["images"] = images
    r = client.post("/api/generate", json=body, timeout=300)
    r.raise_for_status()
    return r.json().get("total_duration", 0) / 1e6


def ollama_ps() -> str:
    r = subprocess.run(["ollama", "ps"], capture_output=True, text=True)
    return r.stdout.strip()


def stats(samples: list[float]) -> tuple[float, float]:
    """median, p95 (ms)."""
    if not samples:
        return 0.0, 0.0
    med = round(statistics.median(samples), 1)
    p95 = round(sorted(samples)[min(len(samples) - 1,
                                     int(round(0.95 * (len(samples) - 1))))], 1)
    return med, p95


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=5)
    args = ap.parse_args()
    runs = args.runs
    OUT.mkdir(exist_ok=True)
    rows = []  # (scenario, model, median_ms, p95_ms, n, gpu_note)

    with httpx.Client(base_url=BASE) as client:
        # --- cold run: free GPU, then first call includes load time ---
        free_gpu()
        cold = [generate(client, PRIMARY, "Say ok.", keep_alive="5m")]
        ps_after_load = ollama_ps()
        print("\n--- ollama ps after cold load ---\n" + ps_after_load)
        med, p95 = stats(cold)
        rows.append(("cold_primary", PRIMARY, med, p95, 1, _gpu_note(ps_after_load, PRIMARY)))

        # --- warm runs: model already loaded ---
        warm = [generate(client, PRIMARY, "Say ok.") for _ in range(runs)]
        med, p95 = stats(warm)
        rows.append(("warm_primary", PRIMARY, med, p95, runs, _gpu_note(ollama_ps(), PRIMARY)))

        # --- vision call (qwen3-vl:2b) ---
        free_gpu()
        try:
            vis = [generate(client, VISION, "What color is this?", images=[VISION_PNG])
                   for _ in range(runs)]
            med, p95 = stats(vis)
            rows.append(("vision", VISION, med, p95, runs, _gpu_note(ollama_ps(), VISION)))
        except httpx.HTTPStatusError as e:
            # Measure, or say it is unmeasured — never invent a number.
            print(f"vision call failed: {e}", file=sys.stderr)
            rows.append(("vision", VISION, "unmeasured", f"HTTP {e.response.status_code}",
                         0, "n/a"))

        # --- 1 vs 2 concurrent (primary, warm) ---
        for conc in (1, 2):
            samples = _concurrent(client, conc, runs)
            med, p95 = stats(samples)
            rows.append((f"concurrent_{conc}", PRIMARY, med, p95, len(samples),
                         _gpu_note(ollama_ps(), PRIMARY)))

    _write_csv(rows)
    print("\n--- summary ---")
    for scenario, model, med, p95, n, note in rows:
        print(f"{scenario:16} {model:18} median={med:>8} ms  p95={p95:>8} ms  n={n}  {note}")


def _concurrent(client: httpx.Client, conc: int, runs: int) -> list[float]:
    samples = []
    for _ in range(runs):
        with cf.ThreadPoolExecutor(max_workers=conc) as ex:
            futs = [ex.submit(generate, client, PRIMARY, "Say ok.") for _ in range(conc)]
            # wall time for the batch, per request
            t0 = time.perf_counter()
            for f in cf.as_completed(futs):
                f.result()
            samples.append((time.perf_counter() - t0) * 1000 / conc)
    return samples


def _gpu_note(ps_text: str, model: str) -> str:
    """Pull the PROCESSOR column (e.g. '100% GPU') for the model from ollama ps."""
    for line in ps_text.splitlines():
        if line.startswith(model):
            m = re.search(r"(\d+)%\s*(GPU|CPU)(?:/(\d+)%\s*(GPU|CPU))?", line)
            return m.group(0) if m else "processor=?"
    return "not loaded"


def _write_csv(rows) -> None:
    path = OUT / f"bench_{int(time.time())}.csv"
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["scenario", "model", "median_ms", "p95_ms", "n", "gpu"])
        w.writerows(rows)
    print(f"\nwrote {path}")


if __name__ == "__main__":
    main()
