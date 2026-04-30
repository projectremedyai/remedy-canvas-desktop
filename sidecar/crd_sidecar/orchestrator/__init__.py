"""Per-course remediation pipeline — parse IMSCC → analyze → transform → write."""

from crd_sidecar.orchestrator.pipeline import (
    RemediationOptions,
    RemediationSummary,
    PageReport,
    remediate_course,
)

__all__ = [
    "RemediationOptions",
    "RemediationSummary",
    "PageReport",
    "remediate_course",
]
