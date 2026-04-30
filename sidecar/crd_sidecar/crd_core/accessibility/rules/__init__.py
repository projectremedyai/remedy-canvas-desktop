# Accessibility rules package
from crd_sidecar.crd_core.accessibility.rules.base import AccessibilityRule
from crd_sidecar.crd_core.accessibility.rules.images import MissingAltTextRule, InadequateAltTextRule
from crd_sidecar.crd_core.accessibility.rules.headings import H1UsedRule, SkippedHeadingLevelRule
from crd_sidecar.crd_core.accessibility.rules.tables import (
    MissingTableHeadersRule,
    MissingScopeAttributeRule,
    MissingTableCaptionRule,
)
from crd_sidecar.crd_core.accessibility.rules.links import NonDescriptiveLinkTextRule
from crd_sidecar.crd_core.accessibility.rules.contrast import InsufficientContrastRule

__all__ = [
    "AccessibilityRule",
    "MissingAltTextRule",
    "InadequateAltTextRule",
    "H1UsedRule",
    "SkippedHeadingLevelRule",
    "MissingTableHeadersRule",
    "MissingScopeAttributeRule",
    "MissingTableCaptionRule",
    "NonDescriptiveLinkTextRule",
    "InsufficientContrastRule",
]
