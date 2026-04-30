"""ACR export service — renders a ``CourseACR`` to HTML / JSON / Markdown.

Vendored from ``lti_app/services/acr_export_service.py``. The Jinja2
``PackageLoader`` is rebound to our own package so the template bundles
with the sidecar wheel / PyInstaller build.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional

from jinja2 import Environment, PackageLoader, select_autoescape

from crd_sidecar.crd_core.models import (
    ConformanceLevel,
    CourseACR,
    CriterionRollup,
)

# Default loader: reads templates/acr_report.html from the packaged sidecar.
_jinja_env = Environment(
    loader=PackageLoader("crd_sidecar", "templates"),
    autoescape=select_autoescape(["html", "xml"]),
)


def _get_conformance_badge_class(conformance: ConformanceLevel) -> str:
    """Tailwind-ish CSS class name for a conformance badge."""
    return {
        ConformanceLevel.SUPPORTS: "bg-green-100 text-green-800",
        ConformanceLevel.PARTIALLY_SUPPORTS: "bg-yellow-100 text-yellow-800",
        ConformanceLevel.DOES_NOT_SUPPORT: "bg-red-100 text-red-800",
        ConformanceLevel.NOT_APPLICABLE: "bg-gray-100 text-gray-800",
    }.get(conformance, "bg-gray-100 text-gray-800")


def _get_conformance_icon(conformance: ConformanceLevel) -> str:
    return {
        ConformanceLevel.SUPPORTS: "\u2705",           # ✅
        ConformanceLevel.PARTIALLY_SUPPORTS: "\u26a0\ufe0f",  # ⚠️
        ConformanceLevel.DOES_NOT_SUPPORT: "\u274c",   # ❌
        ConformanceLevel.NOT_APPLICABLE: "\u2796",     # ➖
    }.get(conformance, "\u2796")


class ACRExportService:
    """Render a CourseACR to multiple formats."""

    def __init__(self, template_dir: Optional[Path] = None):
        if template_dir:
            from jinja2 import FileSystemLoader
            self._env = Environment(
                loader=FileSystemLoader(template_dir),
                autoescape=select_autoescape(["html", "xml"]),
            )
        else:
            self._env = _jinja_env

        self._env.filters["conformance_badge"] = _get_conformance_badge_class
        self._env.filters["conformance_icon"] = _get_conformance_icon

    def export_html(self, acr: CourseACR, include_evidence: bool = True) -> str:
        """Render the ACR as a standalone HTML document."""
        template = self._env.get_template("acr_report.html")

        level_a_criteria = [c for c in acr.criteria if c.level == "A"]
        level_aa_criteria = [c for c in acr.criteria if c.level == "AA"]

        stats = {
            "total_criteria": len(acr.criteria),
            "level_a_total": len(level_a_criteria),
            "level_a_supports": sum(
                1 for c in level_a_criteria
                if c.conformance == ConformanceLevel.SUPPORTS
            ),
            "level_a_partial": sum(
                1 for c in level_a_criteria
                if c.conformance == ConformanceLevel.PARTIALLY_SUPPORTS
            ),
            "level_a_fails": sum(
                1 for c in level_a_criteria
                if c.conformance == ConformanceLevel.DOES_NOT_SUPPORT
            ),
            "level_aa_total": len(level_aa_criteria),
            "level_aa_supports": sum(
                1 for c in level_aa_criteria
                if c.conformance == ConformanceLevel.SUPPORTS
            ),
            "level_aa_partial": sum(
                1 for c in level_aa_criteria
                if c.conformance == ConformanceLevel.PARTIALLY_SUPPORTS
            ),
            "level_aa_fails": sum(
                1 for c in level_aa_criteria
                if c.conformance == ConformanceLevel.DOES_NOT_SUPPORT
            ),
            "total_issues": sum(c.issue_count for c in acr.criteria),
            "total_pages_affected": sum(c.pages_affected for c in acr.criteria),
        }

        criteria_by_category: dict[str, list[CriterionRollup]] = {}
        for criterion in acr.criteria:
            category_id = criterion.criterion_id.rsplit(".", 1)[0]
            criteria_by_category.setdefault(category_id, []).append(criterion)

        return template.render(
            acr=acr,
            stats=stats,
            criteria_by_category=criteria_by_category,
            evidence=acr.evidence,
            include_evidence=include_evidence,
            generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )

    def export_json(self, acr: CourseACR) -> str:
        """Serialise the ACR as pretty-printed JSON."""
        import json
        return json.dumps(acr.model_dump(mode="json"), indent=2, default=str)

    def export_markdown(self, acr: CourseACR) -> str:
        """Render a minimal Markdown version suitable for grep / diff."""
        lines = [
            "# Accessibility Conformance Report",
            "",
            f"**Course:** {acr.course_name}",
            f"**Generated:** {acr.generated_at.strftime('%Y-%m-%d %H:%M:%S')}",
            f"**Evaluator:** {acr.evaluator}",
            f"**VPAT Edition:** {acr.vpat_edition}",
            f"**WCAG Version:** {acr.wcag_version} Level {acr.conformance_level}",
            "",
            "## Executive Summary",
            "",
            f"**Overall Status:** {acr.overall_status.value}",
            f"**Conformance Score:** {acr.conformance_percentage:.1f}%",
            "",
        ]

        if acr.issues_before > 0:
            lines.extend([
                "### Remediation Impact",
                "",
                f"- **Issues Before:** {acr.issues_before}",
                f"- **Issues After:** {acr.issues_after}",
                f"- **Issues Fixed:** {acr.issues_fixed}",
                f"- **Pages Remediated:** {acr.pages_remediated}",
                "",
            ])

        lines.extend([
            "## WCAG Conformance Summary",
            "",
            "| Criterion | Level | Status | Issues | Pages Affected |",
            "|-----------|-------|--------|--------|----------------|",
        ])

        for criterion in acr.criteria:
            lines.append(
                f"| {criterion.criterion_id} {criterion.name} | "
                f"{criterion.level} | {criterion.conformance.value} | "
                f"{criterion.issue_count} | {criterion.pages_affected} |"
            )

        lines.extend([
            "",
            "## Detailed Remarks",
            "",
        ])

        for criterion in acr.criteria:
            if criterion.conformance != ConformanceLevel.SUPPORTS:
                lines.extend([
                    f"### {criterion.criterion_id} {criterion.name}",
                    "",
                    f"**Level:** {criterion.level}",
                    f"**Status:** {criterion.conformance.value}",
                    f"**Issues:** {criterion.issue_count}",
                    "",
                    criterion.remarks,
                    "",
                ])

        return "\n".join(lines)

    def save_html_report(
        self,
        acr: CourseACR,
        output_path: Path,
        include_evidence: bool = True,
    ) -> None:
        html = self.export_html(acr, include_evidence)
        output_path.write_text(html, encoding="utf-8")
