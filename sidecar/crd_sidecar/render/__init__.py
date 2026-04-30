"""Mock-Canvas headless renderer + axe-core runner.

Phase 3 scope — wraps a Canvas-page HTML fragment in a Canvas-mimicking
CSS shell, renders it through headless Chromium (Playwright), runs
axe-core, and maps violations to Remedy Canvas Desktop AccessibilityIssue objects.
"""

from crd_sidecar.render.axe_runner import (
    rendered_scan,
    rendered_scan_async,
)
from crd_sidecar.render.mock_shell import (
    load_canvas_mock_css,
    wrap_in_mock_shell,
)

__all__ = [
    "rendered_scan",
    "rendered_scan_async",
    "wrap_in_mock_shell",
    "load_canvas_mock_css",
]
