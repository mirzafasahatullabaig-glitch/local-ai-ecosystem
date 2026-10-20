# Confirms the required models exist, sends one tiny prompt to the primary,
# and honestly reports whether the primary accepts an image on this build.
$ErrorActionPreference = "Stop"
$ollamaHost = if ($env:OLLAMA_HOST) { $env:OLLAMA_HOST } else { "127.0.0.1:11434" }
$ollamaHost = $ollamaHost -replace '^https?://', ''   # OLLAMA_HOST may already carry a scheme
$base = "http://$ollamaHost"

$required = @(
    "qwen3.5:4b", "llama3.2:3b", "qwen3-vl:2b",
    "qwen2.5-coder:7b", "deepseek-coder:6.7b"
)

Write-Host "== verify_models ==" -ForegroundColor Cyan
$installed = (& ollama list | Select-Object -Skip 1 | ForEach-Object { ($_ -split '\s{2,}')[0].Trim() })

$missing = @()
foreach ($m in $required) {
    if ($installed -contains $m) { Write-Host "  [ok] $m" }
    else { Write-Host "  [MISSING] $m" -ForegroundColor Red; $missing += $m }
}
if ($missing) { Write-Error "Missing models: $($missing -join ', ')"; exit 1 }

# Tiny prompt to the primary.
Write-Host "`nprompting qwen3.5:4b ..."
$body = @{ model = "qwen3.5:4b"; prompt = "Reply with the single word: ok";
           stream = $false; keep_alive = 0 } | ConvertTo-Json
$r = Invoke-RestMethod -Uri "$base/api/generate" -Method Post -Body $body -ContentType "application/json"
Write-Host "  reply: $($r.response.Trim())"

# Image-support probe: send a 1x1 PNG. If Ollama errors, the primary can't see.
# 1x1 transparent PNG, base64.
$png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M8AAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
$imgBody = @{ model = "qwen3.5:4b"; prompt = "What color is this?";
              images = @($png); stream = $false; keep_alive = 0 } | ConvertTo-Json
$supportsImages = "NO"
try {
    $ir = Invoke-RestMethod -Uri "$base/api/generate" -Method Post -Body $imgBody -ContentType "application/json" -ErrorAction Stop
    # Success only counts if Ollama returned a response without an error field.
    if ($ir.response) { $supportsImages = "YES" }
} catch {
    $supportsImages = "NO"
}
Write-Host "`nqwen3.5:4b accepts images on this build: $supportsImages" -ForegroundColor Yellow
Write-Host "(If NO, the vision role uses qwen3-vl:2b.)"
exit 0
