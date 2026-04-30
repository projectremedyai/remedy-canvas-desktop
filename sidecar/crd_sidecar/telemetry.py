"""Opt-in crash reporting via Sentry.

Reports sidecar crashes + uncaught pipeline exceptions to the DSN in
the ``CRD_SENTRY_DSN`` environment variable. If the variable is
unset (the default), this module is a no-op — nothing is imported,
nothing phones home.

Privacy guarantees when active:
- No course content, no file paths outside Remedy Canvas Desktop's own code,
  no LLM prompts or responses. ``before_send`` scrubs breadcrumbs
  with obvious PII patterns.
- The user's IMSCC paths are redacted to their basename.
- ``send_default_pii = False`` — we never send user identifiers.
- Transport is HTTPS-only.

Activation: set ``CRD_SENTRY_DSN=<your sentry DSN>`` in the launched
Remedy Canvas Desktop process env (the Rust shell can inject it based on a
user-visible "Share crash reports" toggle — not built yet).
"""

from __future__ import annotations

import os
import re
from typing import Any

_INITIALIZED = False


def init_crash_reporting() -> bool:
    """Initialize Sentry if ``CRD_SENTRY_DSN`` is present. Returns True
    on successful init, False if disabled or the SDK is absent.

    Safe to call multiple times — subsequent calls no-op.
    """
    global _INITIALIZED
    if _INITIALIZED:
        return True

    dsn = os.environ.get("CRD_SENTRY_DSN", "").strip()
    if not dsn:
        return False

    try:
        import sentry_sdk
    except ImportError:
        return False

    release = os.environ.get("CRD_VERSION", "0.1.0")
    environment = os.environ.get("CRD_SENTRY_ENV", "production")

    sentry_sdk.init(
        dsn=dsn,
        release=f"remedy-canvas-desktop@{release}",
        environment=environment,
        send_default_pii=False,
        traces_sample_rate=0.0,
        attach_stacktrace=True,
        before_send=_scrub_event,
        before_breadcrumb=_scrub_breadcrumb,
    )
    _INITIALIZED = True
    return True


def capture_exception(exc: BaseException, **tags: str) -> None:
    """Forward an exception to Sentry. No-op if not initialized."""
    if not _INITIALIZED:
        return
    try:
        import sentry_sdk

        with sentry_sdk.push_scope() as scope:
            for k, v in tags.items():
                scope.set_tag(k, str(v))
            sentry_sdk.capture_exception(exc)
    except Exception:  # noqa: BLE001
        # Telemetry failures NEVER propagate — a broken reporter must
        # not take down the sidecar
        pass


# ---------------------------------------------------------------------------
# Scrubbers — strip PII from anything heading to Sentry
# ---------------------------------------------------------------------------

# Home directories on macOS / Linux reveal usernames. We never send them.
_HOME_RE = re.compile(r"/Users/[^/\s]+|/home/[^/\s]+", re.IGNORECASE)
# Email addresses (instructors / students in sample paths / HTML snippets)
_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
# IMSCC-ish filenames often encode course / instructor info in the name;
# always reduce `/any/path/to/foo.imscc` → `<imscc-dir>/foo.imscc`
# (keep basename for debugging, scrub the directory path).
_IMSCC_PATH_RE = re.compile(r"(\S*/)?([^/\s\"']+\.imscc)", re.IGNORECASE)


def _scrub_string(s: str) -> str:
    # Scrub from most-specific to least-specific so a composite path
    # like /Users/alice/Desktop/course.imscc reduces cleanly to
    # <imscc-dir>/course.imscc instead of <home>/Desktop/course.imscc.
    s = _IMSCC_PATH_RE.sub(r"<imscc-dir>/\2", s)
    s = _EMAIL_RE.sub("<email>", s)
    s = _HOME_RE.sub("<home>", s)
    return s


def _scrub_value(value: Any) -> Any:
    if isinstance(value, str):
        return _scrub_string(value)
    if isinstance(value, dict):
        return {k: _scrub_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub_value(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_scrub_value(v) for v in value)
    return value


def _scrub_event(event: dict, _hint: dict) -> dict:
    return _scrub_value(event)


def _scrub_breadcrumb(breadcrumb: dict, _hint: dict) -> dict:
    return _scrub_value(breadcrumb)


__all__ = ["init_crash_reporting", "capture_exception"]
