#!/usr/bin/env bash
# Sign + notarize + staple a macOS .app bundle produced by `pnpm tauri build`.
#
# Reads credentials from the environment. Fails fast if any are unset:
#   APPLE_SIGNING_IDENTITY   e.g. "Developer ID Application: Your Team Name (TEAMID)"
#   APPLE_ID                 Apple ID email (developer account)
#   APPLE_PASSWORD           **App-specific password** from appleid.apple.com
#                            (NOT your Apple ID password — generate one at
#                             https://appleid.apple.com → App-Specific Passwords)
#   APPLE_TEAM_ID            10-char alphanumeric team ID (visible at
#                            https://developer.apple.com/account, in the
#                            top-right dropdown)
#
# Optional:
#   DMG_OUTPUT_DIR           Where to write the notarized .dmg
#                            (default: src-tauri/target/release/bundle/dmg)
#
# This is intentionally ONE script that handles every step:
#   1. codesign --deep with hardened runtime + entitlements
#   2. Zip for notarytool submission (notarytool wants .zip/.dmg, not .app)
#   3. xcrun notarytool submit --wait (blocks until Apple approves, ~2-10 min)
#   4. xcrun stapler staple the .app (so Gatekeeper doesn't re-verify online)
#   5. Rebuild the .dmg (hdiutil, same as Phase 7a workaround) and staple it
#
# Produces: a notarized + stapled .app and .dmg. Ready for distribution.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP_NAME="Remedy Canvas Desktop"
APP_PATH="${ROOT}/src-tauri/target/release/bundle/macos/${APP_NAME}.app"
ENTITLEMENTS="${ROOT}/src-tauri/entitlements.plist"

# Default DMG output mirrors Phase 7a's layout.
DMG_OUTPUT_DIR="${DMG_OUTPUT_DIR:-${ROOT}/src-tauri/target/release/bundle/dmg}"
DMG_PATH="${DMG_OUTPUT_DIR}/${APP_NAME}_0.1.0_aarch64.dmg"

# ---------- preflight ------------------------------------------------------

for var in APPLE_SIGNING_IDENTITY APPLE_ID APPLE_PASSWORD APPLE_TEAM_ID; do
    if [ -z "${!var:-}" ]; then
        echo "error: \$${var} is not set" >&2
        echo "see scripts/sign-and-notarize.sh header for how to obtain each credential" >&2
        exit 1
    fi
done

if [ ! -d "${APP_PATH}" ]; then
    echo "error: ${APP_PATH} not found — run \`pnpm tauri build\` first" >&2
    exit 1
fi

if [ ! -f "${ENTITLEMENTS}" ]; then
    echo "error: ${ENTITLEMENTS} not found" >&2
    exit 1
fi

echo "==> signing ${APP_PATH}"
echo "    identity:  ${APPLE_SIGNING_IDENTITY}"
echo "    team:      ${APPLE_TEAM_ID}"

# ---------- signing helper ------------------------------------------------

# codesign_one <path>
# Signs a single bundle, dylib, or Mach-O with hardened runtime,
# entitlements, and an Apple timestamp. Used by every signing pass.
codesign_one() {
    local target="$1"
    codesign \
        --force \
        --options runtime \
        --entitlements "${ENTITLEMENTS}" \
        --sign "${APPLE_SIGNING_IDENTITY}" \
        --timestamp \
        "${target}"
}

# ---------- pass 1: chromium frameworks + helper apps (deepest first) ------

CHROMIUM_DIR="${APP_PATH}/Contents/Resources/vendor/chromium"

if [ -d "${CHROMIUM_DIR}" ]; then
    echo "==> pass 1: signing nested bundles in ${CHROMIUM_DIR}"
    # -print0 / sort -rz handles paths with spaces (e.g. "Chromium Helper.app")
    while IFS= read -r -d '' bundle; do
        echo "    sign bundle: ${bundle#${APP_PATH}/}"
        codesign_one "${bundle}"
    done < <(find "${CHROMIUM_DIR}" \
                  \( -name "*.framework" -o -name "*.app" \) \
                  -print0 | sort -rz)
else
    echo "==> pass 1: skipped (no vendor/chromium dir)"
fi

# ---------- pass 2: loose dylibs/so/Mach-O under Resources/vendor ----------
# The -not -path filters exclude files that are already members of a bundle
# we signed in Pass 1. Re-signing a child invalidates the parent's signature.

VENDOR_DIR="${APP_PATH}/Contents/Resources/vendor"

if [ -d "${VENDOR_DIR}" ]; then
    echo "==> pass 2: signing dylibs/so under ${VENDOR_DIR}"
    while IFS= read -r -d '' lib; do
        echo "    sign lib: ${lib#${APP_PATH}/}"
        codesign_one "${lib}"
    done < <(find "${VENDOR_DIR}" -type f \
                  \( -name "*.dylib" -o -name "*.so" \) \
                  -not -path "*.framework/*" \
                  -not -path "*.app/*" \
                  -print0)

    echo "==> pass 2: signing Mach-O executables under ${VENDOR_DIR}"
    while IFS= read -r -d '' f; do
        # `file` reports "Mach-O" for native binaries; skip everything else.
        if file -b "${f}" | grep -q "Mach-O"; then
            echo "    sign exec: ${f#${APP_PATH}/}"
            codesign_one "${f}"
        fi
    done < <(find "${VENDOR_DIR}" -type f -perm -u+x \
                  -not -path "*.framework/*" \
                  -not -path "*.app/*" \
                  -print0)
else
    echo "==> pass 2: skipped (no vendor dir)"
fi

# ---------- pass 3: Tauri externalBin (the Python sidecar) -----------------
# Tauri places externalBin entries at Contents/MacOS/<basename>, outside the
# Resources/vendor/ tree that Passes 1 and 2 cover.

SIDECAR_PATH="${APP_PATH}/Contents/MacOS/crd-sidecar"

if [ -f "${SIDECAR_PATH}" ]; then
    echo "==> pass 3: signing ${SIDECAR_PATH#${APP_PATH}/}"
    codesign_one "${SIDECAR_PATH}"
else
    echo "error: sidecar not found at ${SIDECAR_PATH}" >&2
    echo "       check Contents/MacOS/ for the actual basename" >&2
    exit 1
fi

# ---------- codesign -------------------------------------------------------

codesign \
    --force \
    --deep \
    --options runtime \
    --entitlements "${ENTITLEMENTS}" \
    --sign "${APPLE_SIGNING_IDENTITY}" \
    --timestamp \
    "${APP_PATH}"

echo "==> verifying signature"
codesign --verify --deep --strict --verbose=2 "${APP_PATH}"

# ---------- notarize -------------------------------------------------------

ZIP_PATH="$(mktemp -t remedy-canvas-desktop).zip"
trap 'rm -f "${ZIP_PATH}"' EXIT

echo "==> zipping for notarytool submission: ${ZIP_PATH}"
ditto -c -k --sequesterRsrc --keepParent "${APP_PATH}" "${ZIP_PATH}"

echo "==> submitting to Apple notary service (blocks until done, usually 2-10 min)"
xcrun notarytool submit "${ZIP_PATH}" \
    --apple-id "${APPLE_ID}" \
    --password "${APPLE_PASSWORD}" \
    --team-id "${APPLE_TEAM_ID}" \
    --wait

echo "==> stapling notarization ticket"
xcrun stapler staple "${APP_PATH}"
xcrun stapler validate "${APP_PATH}"

# ---------- rebuild + staple the dmg --------------------------------------

mkdir -p "${DMG_OUTPUT_DIR}"

if [ -f "${DMG_PATH}" ]; then
    echo "==> removing stale ${DMG_PATH}"
    rm -f "${DMG_PATH}"
fi

echo "==> rebuilding DMG via hdiutil"
hdiutil create \
    -volname "${APP_NAME}" \
    -srcfolder "${APP_PATH}" \
    -ov -format UDZO \
    "${DMG_PATH}"

echo "==> stapling DMG"
xcrun stapler staple "${DMG_PATH}"
xcrun stapler validate "${DMG_PATH}"

echo
echo "==> done"
echo "    notarized .app: ${APP_PATH}"
echo "    notarized .dmg: ${DMG_PATH}"
echo "    size: $(du -sh "${DMG_PATH}" | cut -f1)"
