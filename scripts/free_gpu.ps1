# Frees the GPU before any model load, benchmark or server start.
# 1) stop every loaded Ollama model  2) print VRAM  3) warn (don't kill) on
# other GPU hogs > 1 GB  4) exit non-zero if free VRAM < 5 GB.
$ErrorActionPreference = "Stop"

Write-Host "== free_gpu ==" -ForegroundColor Cyan

# 1) Stop loaded models. `ollama ps` header is skipped; col 1 is the name.
$ps = (& ollama ps) 2>&1 | Select-Object -Skip 1
foreach ($line in $ps) {
    $name = ($line -split '\s{2,}')[0].Trim()
    if ($name) {
        Write-Host "stopping $name"
        & ollama stop $name | Out-Null
    }
}
if (-not $ps) { Write-Host "no models loaded" }

# 2) VRAM used / free
$mem = & nvidia-smi --query-gpu=memory.used,memory.free --format=csv,noheader,nounits
$used, $free = ($mem -split ',').Trim()
Write-Host "VRAM used=${used} MiB  free=${free} MiB"

# 3) Other GPU processes. Warn, never kill.
$procs = & nvidia-smi --query-compute-apps=pid,used_memory,process_name --format=csv,noheader,nounits 2>$null
foreach ($p in $procs) {
    if (-not $p) { continue }
    $pid_, $pmem, $pname = ($p -split ',').Trim()
    # Some drivers report [N/A] for per-process memory; skip those rows.
    if ($pmem -notmatch '^\d+$') { continue }
    if ([int]$pmem -gt 1024 -and $pname -notlike "*ollama*") {
        Write-Warning "Non-Ollama process using ${pmem} MiB: $pname (pid $pid_). Close it yourself; I will not kill it."
    }
}

# 4) Gate on free VRAM.
if ([int]$free -lt 5120) {
    Write-Error "Free VRAM ${free} MiB is below the 5 GB floor."
    exit 1
}
Write-Host "OK: free VRAM ${free} MiB >= 5 GB" -ForegroundColor Green
exit 0
