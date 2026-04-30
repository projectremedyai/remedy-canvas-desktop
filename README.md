# Remedy Canvas Desktop

Standalone native desktop app (macOS + Windows) that remediates Canvas IMSCC course exports for WCAG 2.2 AA accessibility — fully offline.

- Slug: `remedy-canvas-desktop`
- Tauri product name / window title: `Remedy Canvas Desktop`
- Bundle identifier: `com.canvasremedy.desktop` (preserved — changing it would
  break code signing, the updater, and installed app identity)
- LLM path: local/downloaded Ollama only. This app is intentionally excluded
  from any Ollama Cloud migration.

**Status:** macOS release candidate. The macOS `.app` and unsigned test `.dmg`
can be built fully offline after fetching bundled runtimes. Production
distribution still requires Developer ID signing and notarization. Windows MSI
builds remain unsigned and do not yet bundle Ollama/LiteParse.

## Architecture

```
Tauri/Rust shell (WebView UI) ── IPC ── Python sidecar (crd_sidecar engine)
                                            │
                                            ├── bundled Ollama (qwen3.5:4b, downloaded on first launch)
                                            └── bundled LiteParse (PDF/DOCX/PPTX → HTML)
```

## Layout

- `src-tauri/` — Rust/Tauri shell (`crate name: remedy-canvas-desktop`, lib name `crd_lib`),
  bundled runtime startup, signing config
- `src/` — React 19 + TypeScript frontend (Vite)
- `sidecar/` — Python remediation engine (`remedy-canvas-desktop-sidecar`,
  module `crd_sidecar`)
  - `crd_core/` — accessibility analysis, remediation, AI suggestions
  - `imscc/` — IMSCC parser + builder
  - `render/` — mock-Canvas headless renderer
  - `ipc/` — JSON-RPC over stdio with the Tauri shell
  - `orchestrator/` — pipeline coordination
  - `templates/` — Jinja2 report templates (e.g. ACR)
- `vendor/` — bundled runtime artifacts (Ollama, LiteParse, axe-core, canvas-mock CSS)
- `models/` — downloaded model weights (populated at first launch, gitignored)
- `installers/` — built `.dmg` / `.msi` outputs

## Local Development

```bash
cd sidecar
uv sync
uv run python -m crd_sidecar.main
```

## Current Entry Point

The sidecar speaks JSON-RPC over stdio. A smoke test exists:

```bash
cd sidecar
echo '{"jsonrpc":"2.0","id":1,"method":"ping"}' | uv run python -m crd_sidecar.main
```

Expected response:
```
{"jsonrpc":"2.0","id":1,"result":{"ok":true,"service":"crd-sidecar"}}
```

## Production Checks

Run these before cutting a release:

```bash
pnpm install --frozen-lockfile
pnpm build

cd sidecar
uv sync --extra dev --extra render --extra documents
uv run ruff check .
uv run pytest -m "not integration"
uv pip check
cd ..

bash scripts/verify-runtimes.sh
cd src-tauri
cargo check --locked
cargo test --locked
cd ..
pnpm audit --prod
```

## Building the Installer (macOS — bundled runtimes, ad-hoc signed test DMG)

```bash
# 1. Download Ollama + build LiteParse/runtime assets into vendor/.
#    Cached at ~/.cache/remedy-canvas-desktop-fetch/; re-running skips what's already done.
bash scripts/fetch-runtimes.sh
bash scripts/verify-runtimes.sh

# 2. Install sidecar build deps + freeze the Python sidecar.
cd sidecar && uv sync --extra dev --extra render --extra documents
uv run python build_binary.py
cd ..

# 3. Build the Tauri app + DMG.
pnpm install --frozen-lockfile
pnpm tauri build --bundles app

# 4. Ad-hoc sign the bundled .app so macOS treats it as a structurally valid,
#    unsigned-but-present-identity bundle. Doesn't pass Gatekeeper (users still
#    right-click → Open), but stops "no signing identity" warnings.
codesign --force --deep --options runtime \
  --entitlements src-tauri/entitlements.plist \
  --sign - \
  "src-tauri/target/release/bundle/macos/Remedy Canvas Desktop.app"

# 5. Build an unsigned test DMG without relying on Finder/AppleScript.
mkdir -p src-tauri/target/release/bundle/dmg
hdiutil create \
  -volname "Remedy Canvas Desktop" \
  -srcfolder "src-tauri/target/release/bundle/macos/Remedy Canvas Desktop.app" \
  -ov -format UDZO \
  "src-tauri/target/release/bundle/dmg/Remedy Canvas Desktop_0.1.0_aarch64.dmg"
```

The current macOS build produces:

- `src-tauri/target/release/bundle/macos/Remedy Canvas Desktop.app` — ~937 MB bundle,
  with `vendor/ollama/`, `vendor/liteparse/`, and `vendor/chromium/` living
  inside `Contents/Resources/`.
- `src-tauri/target/release/bundle/dmg/Remedy Canvas Desktop_0.1.0_aarch64.dmg` —
  ~428 MB installer after UDZO compression.

### DMG-creation gotcha

Tauri's DMG bundler uses AppleScript + Finder to lay out icons; this
intermittently fails with `Finder got an error: AppleEvent timed out. (-1712)`
on macOS, especially in non-interactive shells. If that happens, build the DMG
manually:

```bash
hdiutil create \
  -volname "Remedy Canvas Desktop" \
  -srcfolder "src-tauri/target/release/bundle/macos/Remedy Canvas Desktop.app" \
  -ov -format UDZO \
  "src-tauri/target/release/bundle/dmg/Remedy Canvas Desktop_0.1.0_aarch64.dmg"
```

### What's bundled

- **Ollama** — downloaded from `https://ollama.com/download/Ollama-darwin.zip`
  into `vendor/ollama/`. The app launches its own `ollama serve` on a free
  local port at startup. The default model (`qwen3.5:4b`, ~3.4 GB) is *not*
  bundled — the UI prompts the user to download it on first launch, with
  progress streamed from `/api/pull` over the `ollama.model.pull.progress`
  event. Model weights persist under
  `~/Library/Application Support/com.canvasremedy.desktop/ollama-models/`.
- **LiteParse** — installed via `npm install @llamaindex/liteparse` into
  `vendor/liteparse/node_modules/`, with a portable Node 20 runtime and a
  `liteparse` shell shim alongside. The Rust shell prepends `vendor/liteparse/`
  to `PATH` when spawning the Python sidecar, so the `liteparse` Python
  wrapper finds our shim via `shutil.which("liteparse")`.
- **Playwright `chromium-headless-shell`** — installed via
  `playwright install chromium-headless-shell` into `vendor/chromium/`
  (~190 MB). The Rust shell sets `PLAYWRIGHT_BROWSERS_PATH` on the sidecar
  child in release builds, and `render/axe_runner.py` launches with
  `channel="chromium-headless-shell"`. No post-install browser download.
  `vendor/chromium/.playwright-version` pins the install to the `playwright`
  Python package version so `scripts/verify-runtimes.sh` can detect drift.
- **Python sidecar** — frozen via PyInstaller (~24 MB).
- **axe.min.js + canvas-mock.css** — in `vendor/` already.

### Runtime-pin hygiene

`scripts/fetch-runtimes.sh` writes `vendor/chromium/.playwright-version`
with the exact `playwright` PyPI version the download matched. When you
bump the `playwright>=...` constraint in `sidecar/pyproject.toml`, the
manifest stops matching the installed version and must be refreshed.

Run `bash scripts/verify-runtimes.sh` as the first step in any release
build (and as a CI gate when the Windows workflow grows runtime bundling).
Exits non-zero with an actionable error if:

- `vendor/chromium/` is missing
- `vendor/chromium/.playwright-version` is missing
- the pinned version doesn't match the currently installed `playwright`
- no `chromium_headless_shell-*` dir exists under `vendor/chromium/`

Remediation is always the same: re-run `scripts/fetch-runtimes.sh`, which
is idempotent and skips already-pinned artifacts.

### Known limitations

- **Ad-hoc signed by default.** For production distribution, use the
  Developer ID signing + notarization flow described below.
- **Full Chromium is NOT bundled** — we ship only Playwright's
  `chromium-headless-shell` variant (~190 MB incl. its bundled ffmpeg).
  `include_rendered_scan` works offline out of the box. The full browser
  would add ~300 MB with no user-facing benefit for headless DOM scans.
- **Windows runtime bundling is a TODO.** The CI workflow at
  `.github/workflows/build-windows.yml` produces an unsigned `.msi` with
  the Python sidecar but WITHOUT Ollama or LiteParse bundled. Windows
  end users must install Ollama themselves until
  `scripts/fetch-runtimes.ps1` is expanded to extract Ollama from its
  NSIS installer.

## Developer ID signing + notarization (production macOS builds)

Once the Apple Developer credentials below are in the environment,
`scripts/sign-and-notarize.sh` runs the full codesign → notarytool →
stapler chain + rebuilds the DMG. Blocks until Apple approves
(~2–10 min).

```bash
export APPLE_SIGNING_IDENTITY="Developer ID Application: Your Team Name (TEAMID12345)"
export APPLE_ID="you@example.com"
export APPLE_PASSWORD="<app-specific password from appleid.apple.com>"
export APPLE_TEAM_ID="TEAMID12345"

pnpm tauri build
bash scripts/sign-and-notarize.sh
```

**Credential sources:**
- `APPLE_SIGNING_IDENTITY`: `security find-identity -v -p codesigning` lists
  your installed Developer ID certificates. Copy the full subject line.
- `APPLE_PASSWORD`: generate an **app-specific password** at
  [appleid.apple.com → App-Specific Passwords](https://appleid.apple.com).
  Do NOT use your Apple ID password.
- `APPLE_TEAM_ID`: visible at [developer.apple.com/account](https://developer.apple.com/account)
  in the top-right membership details dropdown.

The entitlements at `src-tauri/entitlements.plist` grant just what the
hardened runtime needs for our embedded sidecar + bundled Ollama and
LiteParse child processes (`disable-library-validation`,
`allow-unsigned-executable-memory`, network client + server). Keep the
list minimal when editing — each entitlement is a surface Apple's
notary and future macOS versions can second-guess.

## Windows builds

`.github/workflows/build-windows.yml` produces an unsigned `.msi` on
every push that touches the frontend, sidecar, or workflow itself.
Download from the workflow's artifacts. Known gaps:

- `scripts/fetch-runtimes.ps1` is a scaffold — it creates empty
  `vendor/ollama/` and `vendor/liteparse/` dirs so the Tauri bundler
  doesn't fail. Real Ollama/LiteParse bundling for Windows is a future
  session (needs OllamaSetup.exe extraction + portable Node for
  LiteParse).
- No code signing. When a Windows signing cert is wired into GitHub
  secrets, set `TAURI_WINDOWS_SIGN_COMMAND` in the workflow to a
  `signtool.exe sign /...` invocation.

### Runtime environment variables

The Rust shell respects these at launch:

- `CRD_OLLAMA_BASE_URL` — override the Ollama base URL. The Python
  sidecar expects the OpenAI-compat `/v1` suffix; the Rust native-API check
  strips it automatically.
- `CRD_SIDECAR_PYTHON` — **dev builds only.** Override the Python
  interpreter used in `pnpm tauri dev`. Release builds ignore this and spawn
  the bundled PyInstaller binary via Tauri's `externalBin`.
