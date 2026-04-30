"""Headless-browser + axe-core rendered accessibility scanner.

Renders an HTML page through Chromium (via Playwright), injects the vendored
axe-core, runs a scan, and maps the violations into Remedy Canvas Desktop AccessibilityIssue
objects using the existing axe_mapper.

This is the standalone-app replacement for Canvas Remedy-LTI's rendered_scanner.py,
which used Canvas session tokens to scan live Canvas pages. Here, we render
locally against our canvas-mock shell — no Canvas login required.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from crd_sidecar.crd_core.accessibility.axe_mapper import (
    axe_violation_to_issues,
)
from crd_sidecar.crd_core.models import AccessibilityIssue
from crd_sidecar.render.mock_shell import wrap_in_mock_shell

# Playwright is an optional extra — import lazily so base-install users
# without chromium can still run the body-only analyzer.
_PLAYWRIGHT_IMPORT_ERROR: str | None = None
try:
    from playwright.async_api import async_playwright  # type: ignore
except ImportError as exc:  # noqa: BLE001
    async_playwright = None  # type: ignore
    _PLAYWRIGHT_IMPORT_ERROR = str(exc)


_AXE_INVOCATION = """
async () => {
    const results = await axe.run(document, {
        resultTypes: ["violations"],
        rules: {
            // Skip rules that fire on our mock shell's structural chrome
            // but aren't about the page content itself.
            "landmark-one-main": {enabled: false},
            "region": {enabled: false},
            "page-has-heading-one": {enabled: false}
        }
    });
    return results.violations;
}
"""


def _locate_axe_js() -> Path:
    """Locate vendored axe-core, trying the bundled app path first and the
    repo checkout as fallback.

    Resolution order mirrors mock_shell.load_canvas_mock_css:
        1. `$CRD_VENDOR_DIR` env var (release builds — set by Rust)
        2. The repo root `vendor/` dir (dev checkout)
    """
    tried: list[Path] = []
    env_dir = os.environ.get("CRD_VENDOR_DIR")
    if env_dir:
        tried.append(Path(env_dir) / "axe.min.js")
    # Dev: sidecar/crd_sidecar/render/axe_runner.py → parents[3] = repo root
    tried.append(Path(__file__).resolve().parents[3] / "vendor" / "axe.min.js")
    for candidate in tried:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        "Vendored axe-core not found. Looked in: "
        + ", ".join(str(p) for p in tried)
    )


# When the Rust shell spawns the sidecar in a packaged build it prepends
# PLAYWRIGHT_BROWSERS_PATH with vendor/chromium. Playwright reads that env
# var at import + launch time. We only use the lean chromium-headless-shell
# channel (~80 MB) — not full Chromium — because rendered accessibility
# scans never need UI chrome, dev tools, or media pipelines. Keeping the
# install footprint lean also keeps the .dmg well under the 500 MB ceiling.
_HEADLESS_CHANNEL = "chromium-headless-shell"


async def rendered_scan_async(
    html_body: str,
    *,
    page_id: str = "adhoc",
    title: str = "Canvas Page",
) -> list[AccessibilityIssue]:
    """Render the given HTML in a mock-Canvas shell, run axe, return issues."""
    if async_playwright is None:
        raise RuntimeError(
            "playwright is not installed. Install the 'render' extra:\n"
            "    uv sync --extra render\n"
            "    uv run playwright install chromium-headless-shell\n"
            f"Underlying import error: {_PLAYWRIGHT_IMPORT_ERROR}"
        )

    shell_html = wrap_in_mock_shell(html_body, title=title)
    axe_js = _locate_axe_js().read_text(encoding="utf-8")

    async with async_playwright() as p:
        # channel="chromium-headless-shell" instructs Playwright to launch
        # the bundled headless-shell binary under PLAYWRIGHT_BROWSERS_PATH,
        # which in the packaged .app points at vendor/chromium/. In dev
        # builds PLAYWRIGHT_BROWSERS_PATH is usually unset, so Playwright
        # falls through to ~/Library/Caches/ms-playwright/.
        browser = await p.chromium.launch(headless=True, channel=_HEADLESS_CHANNEL)
        try:
            context = await browser.new_context()
            page = await context.new_page()
            # data:text/html loads instantly without needing an HTTP server.
            # For complex pages we'd switch to a file:// or local server,
            # but wiki pages fit well within URL limits.
            await page.set_content(shell_html, wait_until="load")
            await page.add_script_tag(content=axe_js)
            violations = await page.evaluate(_AXE_INVOCATION)
        finally:
            await browser.close()

    # axe_violation_to_issues wants a page_id and a "full_url" — we pass a
    # synthetic URL so the downstream UI has something stable to show.
    full_url = f"remedy-canvas-desktop://rendered/{page_id}"
    issues: list[AccessibilityIssue] = []
    for v in violations or []:
        issues.extend(axe_violation_to_issues(v, page_id, full_url))
    return issues


def rendered_scan(
    html_body: str,
    *,
    page_id: str = "adhoc",
    title: str = "Canvas Page",
) -> list[AccessibilityIssue]:
    """Synchronous wrapper for callers outside an event loop."""
    return asyncio.run(rendered_scan_async(html_body, page_id=page_id, title=title))
