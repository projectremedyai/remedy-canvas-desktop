"""Data classes for parsed IMSCC archives."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from crd_sidecar.crd_core.models import CoursePage


@dataclass
class IMSCCResource:
    """A <resource> entry from imsmanifest.xml."""

    identifier: str
    type: str
    href: str | None
    files: list[str] = field(default_factory=list)


@dataclass
class ParsedIMSCC:
    """Output of `parser.parse()` — everything the builder needs to round-trip."""

    path: Path
    course_title: str
    schema_version: str
    # HTML-bearing pages that the remediation pipeline operates on.
    # Phase 2 covers wiki pages; Phase 2.1 adds assignments/discussions/quizzes/syllabus.
    pages: list[CoursePage]
    # Full manifest resource inventory — used by the builder to understand
    # which files are remediation targets vs. inert passthrough (images, PDFs,
    # course_settings XML, etc.).
    resources: list[IMSCCResource]
    # The manifest XML as bytes, so the builder can write it back unchanged
    # (or, in later phases, modify it to register new wiki pages for PDF→HTML
    # conversions).
    manifest_bytes: bytes
