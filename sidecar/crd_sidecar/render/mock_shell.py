"""Wrap a Canvas-page HTML fragment in our Canvas-mimicking render shell.

This gives Playwright + axe a Canvas-like visual context so contrast, focus,
and target-size checks reflect what instructors actually see in production
Canvas — without needing to log into Canvas.
"""

from __future__ import annotations

import os
from pathlib import Path


_SHELL_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
{css}
</style>
</head>
<body>
<div class="ic-app">
  <div class="ic-app-main-content">
    <article class="user_content">
{body}
    </article>
  </div>
</div>
</body>
</html>"""


def _candidate_vendor_dirs(override: Path | None) -> list[Path]:
    """Vendor-dir lookup order: explicit arg, packaged-app env var, dev fallback.

    Keeps bundled + PyInstaller + repo-checkout flows all working without
    per-environment code paths.
    """
    candidates: list[Path] = []
    if override is not None:
        candidates.append(Path(override))
    env_dir = os.environ.get("CRD_VENDOR_DIR")
    if env_dir:
        candidates.append(Path(env_dir))
    # Dev fallback: sidecar/crd_sidecar/render/mock_shell.py
    # → parents[3] = repo root → /vendor.
    candidates.append(Path(__file__).resolve().parents[3] / "vendor")
    return candidates


def load_canvas_mock_css(vendor_dir: Path | None = None) -> str:
    """Read the bundled canvas-mock.css.

    Resolution order:
        1. `vendor_dir` explicit arg
        2. `$CRD_VENDOR_DIR` env var (set by the Rust shell in
           release builds to the app's bundled Resources/vendor/)
        3. The repo root `vendor/` dir (dev checkout)
    """
    tried: list[str] = []
    for base in _candidate_vendor_dirs(vendor_dir):
        css_path = base / "canvas-mock.css"
        tried.append(str(css_path))
        if css_path.is_file():
            return css_path.read_text(encoding="utf-8")
    raise FileNotFoundError(
        "canvas-mock.css not found — looked in: " + ", ".join(tried)
    )


def wrap_in_mock_shell(
    body_html: str,
    *,
    title: str = "Canvas Page",
    css: str | None = None,
) -> str:
    """Return a self-contained HTML document ready for Playwright + axe.

    Accepts either a full HTML document (in which case we extract `<body>`
    content) or a naked fragment. Canvas doesn't serve wiki pages inside
    a `<head>`/`<body>` structure when rendering them through the page
    viewer, so normalizing here keeps axe's findings consistent.
    """
    body = _extract_body(body_html)
    rendered_css = css if css is not None else load_canvas_mock_css()
    return _SHELL_TEMPLATE.format(title=_escape(title), css=rendered_css, body=body)


def _extract_body(html_text: str) -> str:
    """If the input is a full HTML document, return its `<body>` contents.
    Otherwise return it unchanged.
    """
    lowered = html_text.lower()
    body_start = lowered.find("<body")
    if body_start == -1:
        return html_text
    body_open_end = lowered.find(">", body_start)
    if body_open_end == -1:
        return html_text
    body_close = lowered.find("</body>", body_open_end)
    if body_close == -1:
        return html_text[body_open_end + 1 :]
    return html_text[body_open_end + 1 : body_close]


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
