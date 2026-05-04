# fetch-runtimes.ps1 — Windows-side runtime fetcher.
#
# Status: **SCAFFOLD ONLY.** Mirrors scripts/fetch-runtimes.sh on macOS but
# the Windows Ollama distribution ships as an NSIS installer (OllamaSetup.exe)
# rather than a standalone zip. Proper bundling requires either:
#   (a) extracting ollama.exe from inside the installer at fetch time, or
#   (b) shipping the installer as a .msi payload and chaining it at first run
#
# Neither path is implemented yet. This script exists so the Windows CI job
# doesn't fall over with "file not found" if someone naively mirrors the
# macOS build flow; it writes empty vendor/ollama, vendor/liteparse, and
# vendor/chromium folders with a README stub explaining the gap.
#
# When we revisit Windows bundling (Phase 7B-Windows), the likely approach:
#   - Download OllamaSetup.exe from https://ollama.com/download/OllamaSetup.exe
#   - 7z e OllamaSetup.exe -ovendor\ollama\  (extracts ollama.exe + deps)
#   - For LiteParse: install a portable Node via nvm-windows or scoop, then
#     `pnpm add @llamaindex/liteparse` into vendor\liteparse\node_modules\
#   - Both paths are involved enough they deserve their own session.

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$vendor = Join-Path $root "vendor"
New-Item -ItemType Directory -Force -Path $vendor | Out-Null

$ollamaDir = Join-Path $vendor "ollama"
$liteparseDir = Join-Path $vendor "liteparse"
$chromiumDir = Join-Path $vendor "chromium"
New-Item -ItemType Directory -Force -Path $ollamaDir | Out-Null
New-Item -ItemType Directory -Force -Path $liteparseDir | Out-Null
New-Item -ItemType Directory -Force -Path $chromiumDir | Out-Null

$stub = @"
Placeholder — see scripts/fetch-runtimes.ps1 header.

The Windows build currently does NOT bundle Ollama, LiteParse, or Chromium.
End users must install required runtimes separately until Phase 7B-Windows lands.
"@

Set-Content -Path (Join-Path $ollamaDir "README-NOT-BUNDLED.txt") -Value $stub
Set-Content -Path (Join-Path $liteparseDir "README-NOT-BUNDLED.txt") -Value $stub
Set-Content -Path (Join-Path $chromiumDir "README-NOT-BUNDLED.txt") -Value $stub

Write-Host "[fetch-runtimes.ps1] No runtimes fetched — Windows bundling is a TODO."
Write-Host "                     See script header for the implementation plan."
