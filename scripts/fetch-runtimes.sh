#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# fetch-runtimes.sh — Phase 7B external-runtime fetcher.
#
# Populates vendor/ollama/ and vendor/liteparse/ with binaries that ship
# inside the .app bundle. Cached under ~/.cache/remedy-canvas-desktop-fetch/ so
# repeat builds don't re-download hundreds of MB.
#
# macOS arm64 only for Phase 7B; Windows/x86 support is TBD.
# ---------------------------------------------------------------------------
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENDOR="$ROOT/vendor"
CACHE="${CRD_FETCH_CACHE:-$HOME/.cache/remedy-canvas-desktop-fetch}"
mkdir -p "$VENDOR" "$CACHE"

OS="$(uname -s)"
ARCH="$(uname -m)"

log() { printf '[fetch-runtimes] %s\n' "$*"; }
die() { printf '[fetch-runtimes] ERROR: %s\n' "$*" >&2; exit 1; }

if [[ "$OS" != "Darwin" ]]; then
    die "Only macOS is supported in Phase 7B (got $OS)"
fi

# ---------------------------------------------------------------------------
# 1. Ollama — download the official macOS .zip and extract the CLI + libs.
# ---------------------------------------------------------------------------
OLLAMA_URL="https://ollama.com/download/Ollama-darwin.zip"
OLLAMA_ZIP="$CACHE/Ollama-darwin.zip"
OLLAMA_OUT="$VENDOR/ollama"

fetch_ollama() {
    if [[ -x "$OLLAMA_OUT/ollama" ]]; then
        log "Ollama already present at $OLLAMA_OUT (skipping download)"
        return
    fi

    if [[ ! -f "$OLLAMA_ZIP" ]]; then
        log "Downloading Ollama from $OLLAMA_URL"
        curl -fL --retry 3 --progress-bar -o "$OLLAMA_ZIP.part" "$OLLAMA_URL"
        mv "$OLLAMA_ZIP.part" "$OLLAMA_ZIP"
    else
        log "Using cached zip at $OLLAMA_ZIP"
    fi

    log "Extracting Ollama"
    rm -rf "$OLLAMA_OUT"
    mkdir -p "$OLLAMA_OUT"
    TMPDIR_EX="$(mktemp -d)"
    trap 'rm -rf "$TMPDIR_EX"' RETURN
    /usr/bin/unzip -q "$OLLAMA_ZIP" -d "$TMPDIR_EX"

    # The zip contains Ollama.app; we only want the CLI + its sibling dylibs
    # from Contents/Resources/.
    APP_RES="$TMPDIR_EX/Ollama.app/Contents/Resources"
    if [[ ! -d "$APP_RES" ]]; then
        die "Expected Ollama.app/Contents/Resources in the zip; layout changed?"
    fi
    cp "$APP_RES/ollama" "$OLLAMA_OUT/ollama"
    chmod +x "$OLLAMA_OUT/ollama"
    # Copy the GGML dylibs/so files the CLI loads at runtime. They must sit
    # next to the binary (the loader looks in the same directory).
    find "$APP_RES" -maxdepth 1 \( -name '*.dylib' -o -name '*.so' \) -print0 \
        | xargs -0 -I{} cp {} "$OLLAMA_OUT/"
    # Metal shader bundles used for GPU inference on Apple Silicon.
    for dir in mlx_metal_v3 mlx_metal_v4; do
        if [[ -d "$APP_RES/$dir" ]]; then
            cp -R "$APP_RES/$dir" "$OLLAMA_OUT/$dir"
        fi
    done

    log "Smoke-testing bundled Ollama"
    "$OLLAMA_OUT/ollama" --version >/dev/null 2>&1 || die "ollama --version failed"
    log "Ollama OK → $OLLAMA_OUT/ollama"
}

# ---------------------------------------------------------------------------
# 2. LiteParse — bundle a portable Node runtime + an npm-installed copy of
#    @llamaindex/liteparse in vendor/liteparse/, with a shim named `liteparse`
#    so the Python wrapper's `shutil.which("liteparse")` locates it via PATH.
#
#    We tried `pkg` / `@yao-pkg/pkg` first, but @llamaindex/liteparse has two
#    native dependencies (`sharp`, `@hyzyla/pdfium`) that pkg cannot inline.
#    Shipping a portable Node runtime + real node_modules is the only path
#    that works end-to-end without asking the user to install Node.
# ---------------------------------------------------------------------------
LITEPARSE_OUT="$VENDOR/liteparse"
LITEPARSE_BIN="$LITEPARSE_OUT/liteparse"
NODE_VERSION="${CRD_NODE_VERSION:-20.19.1}"

fetch_liteparse() {
    if [[ -x "$LITEPARSE_BIN" ]] && [[ -x "$LITEPARSE_OUT/node/bin/node" ]]; then
        log "LiteParse already present at $LITEPARSE_OUT (skipping)"
        return
    fi

    rm -rf "$LITEPARSE_OUT"
    mkdir -p "$LITEPARSE_OUT"

    # --- 2a. Portable Node runtime --------------------------------------
    NODE_ARCH="arm64"
    case "$ARCH" in
        arm64|aarch64) NODE_ARCH="arm64" ;;
        x86_64)        NODE_ARCH="x64" ;;
        *) die "unsupported arch for Node: $ARCH" ;;
    esac
    NODE_TARBALL_NAME="node-v${NODE_VERSION}-darwin-${NODE_ARCH}.tar.xz"
    NODE_URL="https://nodejs.org/dist/v${NODE_VERSION}/${NODE_TARBALL_NAME}"
    NODE_TARBALL="$CACHE/$NODE_TARBALL_NAME"

    if [[ ! -f "$NODE_TARBALL" ]]; then
        log "Downloading Node $NODE_VERSION ($NODE_ARCH) from $NODE_URL"
        curl -fL --retry 3 --progress-bar -o "$NODE_TARBALL.part" "$NODE_URL"
        mv "$NODE_TARBALL.part" "$NODE_TARBALL"
    else
        log "Using cached Node tarball at $NODE_TARBALL"
    fi

    log "Extracting Node runtime"
    mkdir -p "$LITEPARSE_OUT/node"
    tar -xJf "$NODE_TARBALL" -C "$LITEPARSE_OUT/node" --strip-components=1

    # --- 2b. Install @llamaindex/liteparse into vendor/liteparse/node_modules
    log "Installing @llamaindex/liteparse (this compiles native deps)"
    (
        cd "$LITEPARSE_OUT"
        cat > package.json <<JSON
{
  "name": "remedy-canvas-desktop-liteparse-runtime",
  "version": "1.0.0",
  "private": true,
  "dependencies": {
    "@llamaindex/liteparse": "^1.5.0"
  }
}
JSON
        # Use the bundled Node we just extracted so we don't accidentally
        # compile against a different system Node ABI.
        export PATH="$LITEPARSE_OUT/node/bin:$PATH"
        "$LITEPARSE_OUT/node/bin/npm" install --no-audit --no-fund --loglevel=warn
    )

    if [[ ! -d "$LITEPARSE_OUT/node_modules/@llamaindex/liteparse" ]]; then
        die "npm install did not create node_modules/@llamaindex/liteparse"
    fi

    # --- 2c. Shim called `liteparse` so the Python wrapper's shutil.which()
    #         finds it via PATH after we prepend this dir. The shim invokes
    #         the bundled Node against the installed CLI entrypoint.
    CLI_ENTRY="$LITEPARSE_OUT/node_modules/@llamaindex/liteparse/dist/src/index.js"
    if [[ ! -f "$CLI_ENTRY" ]]; then
        # Fall back to whatever `bin` points at if the package layout moves.
        ALT_ENTRY="$(node -e 'const pkg=require("'"$LITEPARSE_OUT"'/node_modules/@llamaindex/liteparse/package.json"); const bins=pkg.bin||{}; const p=typeof bins==="string"?bins:(bins.liteparse||bins.lit||""); process.stdout.write(p);')"
        if [[ -n "$ALT_ENTRY" ]]; then
            CLI_ENTRY="$LITEPARSE_OUT/node_modules/@llamaindex/liteparse/$ALT_ENTRY"
        fi
    fi
    if [[ ! -f "$CLI_ENTRY" ]]; then
        die "liteparse CLI entrypoint not found under $LITEPARSE_OUT/node_modules/@llamaindex/liteparse"
    fi

    cat > "$LITEPARSE_BIN" <<SHIM
#!/usr/bin/env bash
# Remedy Canvas Desktop — liteparse shim. Invokes the bundled Node runtime against the
# installed @llamaindex/liteparse CLI. The Python wrapper resolves this via
# PATH (shutil.which("liteparse")) once the Rust shell prepends vendor/liteparse/
# to the sidecar's environment.
DIR="\$(cd "\$(dirname "\$0")" && pwd)"
exec "\$DIR/node/bin/node" "\$DIR/node_modules/@llamaindex/liteparse/$(python3 -c "import os; print(os.path.relpath('$CLI_ENTRY', '$LITEPARSE_OUT/node_modules/@llamaindex/liteparse'))")" "\$@"
SHIM
    chmod +x "$LITEPARSE_BIN"

    log "Smoke-testing bundled LiteParse"
    # LiteParse's CLI uses commander, so `--help` exits 0 and writes to stdout.
    if "$LITEPARSE_BIN" --help >/dev/null 2>&1; then
        log "LiteParse OK → $LITEPARSE_BIN"
    else
        log "WARNING: liteparse --help returned non-zero; the binary may still work at runtime"
    fi
}

# ---------------------------------------------------------------------------
# 3. Playwright chromium-headless-shell — the lean (~80 MB) rendering engine
#    we actually use in render/axe_runner.py. Version-locked to whatever
#    `playwright` Python package the sidecar currently depends on, so
#    swapping Python SDK versions forces a matching browser refetch.
# ---------------------------------------------------------------------------
CHROMIUM_OUT="$VENDOR/chromium"
CHROMIUM_MANIFEST="$CHROMIUM_OUT/.playwright-version"

resolve_playwright_version() {
    # Walk the sidecar's dev env and ask playwright itself which version is
    # installed. uv sync --extra render must have run at least once.
    (
        cd "$ROOT/sidecar"
        uv run --extra render python -c \
            "import playwright, importlib.metadata as m; print(m.version('playwright'))"
    )
}

fetch_chromium() {
    # Compute the expected version from the live Python env. If the manifest
    # on disk matches AND the shell binary is present, skip.
    local want_version
    want_version="$(resolve_playwright_version)"
    if [[ -z "$want_version" ]]; then
        die "could not determine installed playwright version — run 'uv sync --extra render' in sidecar/ first"
    fi

    local have_version=""
    if [[ -f "$CHROMIUM_MANIFEST" ]]; then
        have_version="$(cat "$CHROMIUM_MANIFEST" 2>/dev/null || true)"
    fi

    if [[ "$have_version" == "$want_version" ]] \
       && compgen -G "$CHROMIUM_OUT/chromium_headless_shell-*" >/dev/null; then
        log "chromium-headless-shell already at pinned version $want_version (skipping)"
        return
    fi

    log "Installing chromium-headless-shell for playwright $want_version"
    rm -rf "$CHROMIUM_OUT"
    mkdir -p "$CHROMIUM_OUT"

    # PLAYWRIGHT_BROWSERS_PATH redirects the install target. `playwright
    # install` downloads the version matching the installed Python package,
    # so `want_version` drift + missing manifest = forced re-download.
    (
        cd "$ROOT/sidecar"
        PLAYWRIGHT_BROWSERS_PATH="$CHROMIUM_OUT" \
            uv run --extra render python -m playwright install chromium-headless-shell
    )

    # Sanity check: install puts the shell binaries under
    # chromium_headless_shell-<build>/ inside PLAYWRIGHT_BROWSERS_PATH.
    if ! compgen -G "$CHROMIUM_OUT/chromium_headless_shell-*" >/dev/null; then
        die "playwright install completed but no chromium_headless_shell-* dir found under $CHROMIUM_OUT"
    fi

    printf '%s\n' "$want_version" > "$CHROMIUM_MANIFEST"
    log "chromium-headless-shell OK → $CHROMIUM_OUT (playwright $want_version)"
}

fetch_ollama
fetch_liteparse
fetch_chromium

log "Done. Contents:"
ls -la "$VENDOR/ollama" | head -20
ls -la "$VENDOR/liteparse"
ls -la "$VENDOR/chromium"
