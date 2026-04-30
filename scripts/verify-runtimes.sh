#!/usr/bin/env bash
# verify-runtimes.sh — confirm the bundled runtimes under vendor/ are the
# versions the current sidecar dependencies expect.
#
# Intended to run:
#   - In CI right before `pnpm tauri build` to catch "I bumped playwright
#     but forgot to rerun fetch-runtimes.sh".
#   - Locally by developers before cutting a release.
#
# Exit codes:
#   0  everything matches
#   1  one or more runtimes are stale, missing, or version-drifted
#
# Currently covers: Playwright / chromium-headless-shell version pinning.
# Ollama + LiteParse don't carry in-repo version pins today; when they do,
# add matching checks here.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENDOR="$ROOT/vendor"

fail() { printf '[verify-runtimes] FAIL: %s\n' "$*" >&2; exit 1; }
ok()   { printf '[verify-runtimes] ok:   %s\n' "$*"; }

# ---------------------------------------------------------------------------
# Playwright + chromium-headless-shell
# ---------------------------------------------------------------------------

CHROMIUM_DIR="$VENDOR/chromium"
MANIFEST="$CHROMIUM_DIR/.playwright-version"

if [[ ! -d "$CHROMIUM_DIR" ]]; then
    fail "vendor/chromium/ missing. Run scripts/fetch-runtimes.sh."
fi

if [[ ! -f "$MANIFEST" ]]; then
    fail "vendor/chromium/.playwright-version missing. Run scripts/fetch-runtimes.sh to write it."
fi

HAVE="$(cat "$MANIFEST")"

WANT="$(
    cd "$ROOT/sidecar"
    uv run --extra render python -c \
        "import importlib.metadata as m; print(m.version('playwright'))"
)"

if [[ "$HAVE" != "$WANT" ]]; then
    fail "playwright version mismatch. vendor/chromium was fetched for $HAVE but the sidecar env has $WANT. Run scripts/fetch-runtimes.sh."
fi

if ! compgen -G "$CHROMIUM_DIR/chromium_headless_shell-*" >/dev/null; then
    fail "vendor/chromium/ exists but contains no chromium_headless_shell-* directory. Re-run scripts/fetch-runtimes.sh."
fi

ok "Playwright $HAVE, chromium-headless-shell present"

# ---------------------------------------------------------------------------
# Extend as we add version pins for the other vendored runtimes.
# ---------------------------------------------------------------------------

ok "All runtime pins verified."
