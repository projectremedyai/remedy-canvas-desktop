"""ACR (Accessibility Conformance Report) generation service.

Vendored from ``lti_app/services/acr_service.py`` and decoupled from the
Canvas/LTI database layer. The pure builder function ``build_course_acr``
is what the desktop sidecar calls — it runs in-memory only, no repos.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from typing import Optional

from ulid import ULID

from crd_sidecar.crd_core.accessibility.vpat import get_aa_criteria
from crd_sidecar.crd_core.models import (
    AccessibilityIssue,
    AccessibilityReport,
    ArtifactEvidence,
    ConformanceLevel,
    ContentType,
    CourseACR,
    CriterionRollup,
    FileReport,
    FindingEvidence,
    RemediationStatus,
)


class ACRService:
    """Stateless ACR generator.

    The LTI flavour of this service was repo-coupled (ACRRepository +
    ScanRepository). The desktop build runs in-process with no DB, so we
    keep only the pure helpers. ``build_course_acr`` below is the single
    entry point used by the sidecar.
    """

    _DOC_LINK_CRITERIA = {"1.1.1", "2.4.4"}

    # ------------------------------------------------------------------
    # Pure helpers (unchanged from the LTI version)
    # ------------------------------------------------------------------

    def _build_criteria(
        self,
        report: AccessibilityReport,
        file_report: Optional[FileReport],
    ) -> list[CriterionRollup]:
        """Aggregate issues by WCAG criterion, returning per-criterion rollups."""
        issues_by_criterion: dict[str, list[AccessibilityIssue]] = defaultdict(list)

        for issue in report.issues:
            criterion_id = issue.wcag_criterion
            if criterion_id:
                issues_by_criterion[criterion_id].append(issue)

        # Add file audit issues if available
        if file_report:
            for entry in file_report.entries:
                if entry.is_pdf and entry.status == "failed":
                    # PDF failures typically map to 1.3.1 (Info and Relationships).
                    issues_by_criterion["1.3.1"].append(
                        AccessibilityIssue(
                            id=f"file_{entry.file_id}",
                            rule_id="PDF_CHECK",
                            severity="error",
                            category="structure",
                            wcag_criterion="1.3.1",
                            message=f"PDF '{entry.filename}' failed accessibility checks",
                            page_id=str(entry.file_id),
                            canvas_url=f"/files/{entry.file_id}",
                        )
                    )

        criteria: list[CriterionRollup] = []
        aa_criteria = get_aa_criteria()

        for criterion_def in aa_criteria:
            criterion_id = criterion_def["id"]
            issues = issues_by_criterion.get(criterion_id, [])

            pages_affected = len({issue.page_id for issue in issues})

            # Only course_content issues drive the conformance determination;
            # canvas_platform findings are surfaced in the remarks but don't
            # penalize the score (matches the LTI behaviour).
            content_issues = [
                i for i in issues
                if getattr(i, "source", "course_content") == "course_content"
            ]
            content_pages_affected = len({issue.page_id for issue in content_issues})

            total_pages = report.pages_analyzed
            if not content_issues:
                conformance = ConformanceLevel.SUPPORTS
            elif total_pages and content_pages_affected == total_pages:
                conformance = ConformanceLevel.DOES_NOT_SUPPORT
            else:
                conformance = ConformanceLevel.PARTIALLY_SUPPORTS

            remarks = self._build_remarks(
                criterion_def, issues, conformance, file_report=file_report,
            )

            sample_artifacts = list({issue.page_id for issue in issues})[:3]

            criteria.append(
                CriterionRollup(
                    criterion_id=criterion_id,
                    name=criterion_def["name"],
                    level=criterion_def["level"],
                    conformance=conformance,
                    remarks=remarks,
                    issue_count=len(issues),
                    pages_affected=pages_affected,
                    sample_artifacts=sample_artifacts,
                )
            )

        criteria.sort(key=lambda c: c.criterion_id)
        return criteria

    def _build_evidence(
        self,
        report: AccessibilityReport,
        file_report: Optional[FileReport],
    ) -> list[ArtifactEvidence]:
        """Group issues by page/file into ArtifactEvidence records."""
        evidence_map: dict[str, ArtifactEvidence] = {}

        for issue in report.issues:
            page_id = issue.page_id
            if page_id not in evidence_map:
                evidence_map[page_id] = ArtifactEvidence(
                    artifact_id=page_id,
                    artifact_type=ContentType.WIKI_PAGE,
                    title=issue.page_identifier or page_id,
                    canvas_url=issue.canvas_url or "",
                    findings=[],
                )

            evidence_map[page_id].findings.append(
                FindingEvidence(
                    rule_id=issue.rule_id,
                    wcag_criterion=issue.wcag_criterion,
                    severity=issue.severity,
                    message=issue.message,
                    element_html=issue.element_html,
                    remediation_applied=issue.can_auto_fix,
                    remediation_notes=issue.fix_description,
                )
            )

        if file_report:
            for entry in file_report.entries:
                if entry.is_pdf and entry.status == "failed":
                    file_id = str(entry.file_id)
                    evidence_map[file_id] = ArtifactEvidence(
                        artifact_id=file_id,
                        artifact_type=ContentType.WIKI_PAGE,
                        title=entry.filename,
                        canvas_url=f"/files/{entry.file_id}",
                        content_type_mime=entry.content_type,
                        findings=[
                            FindingEvidence(
                                rule_id="PDF_ACCESSIBILITY",
                                wcag_criterion="1.3.1",
                                severity="error",
                                message="PDF accessibility check failed",
                            )
                        ],
                        remediation_status=RemediationStatus.NOT_REMEDIATED,
                    )

        return list(evidence_map.values())

    def _build_remarks(
        self,
        criterion_def: dict,
        issues: list[AccessibilityIssue],
        conformance: ConformanceLevel,
        file_report: Optional[FileReport] = None,
    ) -> str:
        """Human-readable remarks string for a single criterion rollup."""
        if conformance == ConformanceLevel.SUPPORTS:
            remarks = (
                f"All content meets {criterion_def['id']} "
                f"{criterion_def['name']}. No issues detected."
            )
        else:
            issue_types: dict[str, int] = defaultdict(int)
            for issue in issues:
                issue_types[issue.rule_id] += 1

            top_issues = sorted(
                issue_types.items(), key=lambda x: x[1], reverse=True
            )[:3]
            issue_summary = ", ".join(
                [f"{rule} ({count})" for rule, count in top_issues]
            )

            if conformance == ConformanceLevel.DOES_NOT_SUPPORT:
                remarks = (
                    f"Content does not meet {criterion_def['id']}. "
                    f"{len(issues)} issues found: {issue_summary}."
                )
            else:
                remarks = (
                    f"Content partially meets {criterion_def['id']}. "
                    f"{len(issues)} issues across "
                    f"{len({i.page_id for i in issues})} pages: {issue_summary}."
                )

        content_count = sum(
            1 for i in issues
            if getattr(i, "source", "course_content") == "course_content"
        )
        platform_count = sum(
            1 for i in issues
            if getattr(i, "source", "course_content") == "canvas_platform"
        )

        if platform_count > 0:
            remarks += (
                f" ({content_count} in course content, "
                f"{platform_count} in Canvas platform)"
            )

        if (
            file_report
            and criterion_def.get("id") in self._DOC_LINK_CRITERIA
            and any(
                i.rule_id in ("LNK007", "PDF001", "DOC001") for i in issues
            )
        ):
            breakdown = self._file_outcome_breakdown(file_report)
            if breakdown:
                remarks += f" {breakdown}"

        return remarks

    @staticmethod
    def _file_outcome_breakdown(file_report: FileReport) -> str:
        converted = 0
        pdf_fixed = 0
        audit_passed = 0
        skipped = 0
        skip_reasons: dict[str, int] = defaultdict(int)
        for entry in file_report.entries:
            status = getattr(entry, "remediation_status", None)
            if status == "converted":
                converted += 1
            elif status == "pdf_fixed":
                pdf_fixed += 1
            elif status == "audit_passed":
                audit_passed += 1
            elif status == "skipped":
                skipped += 1
                if entry.skip_reason:
                    skip_reasons[
                        ACRService._classify_skip_reason(entry.skip_reason)
                    ] += 1

        parts: list[str] = []
        if converted:
            parts.append(f"{converted} converted to HTML pages")
        if pdf_fixed:
            parts.append(f"{pdf_fixed} PDF fixed in place")
        if audit_passed:
            parts.append(
                f"{audit_passed} PDF{'s' if audit_passed != 1 else ''} "
                f"passed accessibility audit"
            )
        if skipped:
            if skip_reasons:
                details = ", ".join(
                    f"{n} {reason}" for reason, n in sorted(
                        skip_reasons.items(), key=lambda x: -x[1]
                    )
                )
                parts.append(f"{skipped} skipped ({details})")
            else:
                parts.append(f"{skipped} skipped")

        if not parts:
            return ""
        return "Document remediation outcomes: " + "; ".join(parts) + "."

    @staticmethod
    def _classify_skip_reason(raw: str) -> str:
        text = raw.lower()
        if (
            "keyword" in text
            or "textbook" in text
            or "catalog" in text
            or "manual" in text
        ):
            return "reference-doc keyword"
        if "pages" in text and "threshold" in text:
            return "oversized PDF"
        if "conversion failed" in text or "error" in text:
            return "conversion error"
        return "other"

    def _calculate_overall_status(
        self, criteria: list[CriterionRollup]
    ) -> ConformanceLevel:
        if not criteria:
            return ConformanceLevel.NOT_APPLICABLE

        has_not_supported = any(
            c.conformance == ConformanceLevel.DOES_NOT_SUPPORT for c in criteria
        )
        has_partial = any(
            c.conformance == ConformanceLevel.PARTIALLY_SUPPORTS for c in criteria
        )

        if has_not_supported:
            return ConformanceLevel.PARTIALLY_SUPPORTS
        if has_partial:
            return ConformanceLevel.PARTIALLY_SUPPORTS
        return ConformanceLevel.SUPPORTS


def build_course_acr(
    report: AccessibilityReport,
    *,
    course_id: str,
    course_name: str,
    course_url: str = "",
    evaluator: str = "Remedy Canvas Desktop",
    file_report: Optional[FileReport] = None,
    pre_remediation_report: Optional[AccessibilityReport] = None,
) -> CourseACR:
    """Pure in-memory builder — no DB, no async, no job tracking.

    This is what the desktop RPC handler calls. Returns a fully populated
    ``CourseACR`` that ``ACRExportService`` can serialise to HTML / JSON /
    Markdown.
    """
    svc = ACRService()

    criteria = svc._build_criteria(report, file_report)
    evidence = svc._build_evidence(report, file_report)
    overall_status = svc._calculate_overall_status(criteria)

    issues_before = 0
    issues_after = report.total_issues
    issues_fixed = 0
    if pre_remediation_report is not None:
        issues_before = pre_remediation_report.total_issues
        issues_fixed = max(0, issues_before - issues_after)

    return CourseACR(
        id=str(ULID()),
        course_id=course_id,
        scan_run_id=report.course_id,
        generated_at=datetime.now(UTC),
        course_name=course_name,
        course_url=course_url,
        evaluator=evaluator,
        overall_status=overall_status,
        criteria=criteria,
        evidence=evidence,
        issues_before=issues_before,
        issues_after=issues_after,
        issues_fixed=issues_fixed,
        pages_remediated=sum(
            1 for e in evidence
            if e.remediation_status != RemediationStatus.NOT_REMEDIATED
        ),
    )
