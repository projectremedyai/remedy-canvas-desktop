"""The end-to-end remediation pipeline.

Chains: parse IMSCC → analyze every page → (optional) rendered scan →
transform HTML → Canvas-sanitize → patch pages back → write output IMSCC.

Progress events stream to an optional callback so the Tauri shell can
surface per-page status to the user. A job_id flows with every event
so multi-job support later is non-breaking.

Out of Phase 5 scope (see plan):
- AI alt-text fill (Phase 5.1) — orchestrator emits IMG001 issues unchanged
- Document-to-HTML conversion (Phase 5.5) — PDFs/DOCX/PPTX pass through
- Cancellation (Phase 5.1) — runs to completion
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from bs4 import BeautifulSoup

from crd_sidecar.crd_core.accessibility.analyzer import AccessibilityAnalyzer
from crd_sidecar.crd_core.ai import alt_text as alt_text_module
from crd_sidecar.crd_core.models import (
    AccessibilityIssue,
    ContentType,
    CoursePage,
)
from crd_sidecar.crd_core.remediation.canvas_validator import (
    CanvasHTMLValidator,
)
from crd_sidecar.crd_core.remediation.transformer import HTMLTransformer
from crd_sidecar.crd_core.suggestions import (
    FixSuggestion,
    generate_alt_text_suggestion,
    generate_contrast_suggestion,
    generate_form_label_suggestion,
    generate_link_text_suggestion,
    generate_table_caption_suggestion,
)
from crd_sidecar.imscc import (
    DocumentConversionResult,
    IMSCCImageFetcher,
    ParsedIMSCC,
    build,
    convert_and_rewrite,
    parse,
)


ProgressCallback = Callable[[dict[str, Any]], None]


# Content types the Phase 5 orchestrator can write back to an IMSCC.
# QUIZ is parse-only (mattext chunks can't be safely split back from a
# single html_content blob yet — Phase 5.1).
_WRITABLE_TYPES: frozenset[ContentType] = frozenset(
    {
        ContentType.WIKI_PAGE,
        ContentType.ASSIGNMENT,
        ContentType.SYLLABUS,
        ContentType.DISCUSSION,
        ContentType.ANNOUNCEMENT,
    }
)


@dataclass(frozen=True)
class RemediationOptions:
    """Toggles for the orchestrator — each opt-in path adds cost."""

    include_rendered_scan: bool = False
    """Run Playwright + axe against the mock Canvas shell.
    Catches CSS-contrast / focus / target-size that the body scanner can't.
    Adds ~0.5s per page and requires the `render` extra."""

    include_quiz_pages: bool = False
    """Parse-only for quizzes today — the builder refuses to patch them.
    Setting this to True includes quiz pages in the scan counts but skips
    them during write-back."""

    sanitize_after_transform: bool = True
    """Run CanvasHTMLValidator.sanitize() before patching back. Strips
    tags outside the Canvas-allowlist — same guarantee the LTI enforced
    on every write-back. Leave this on unless you're debugging."""

    include_document_conversion: bool = False
    """Convert every bundled PDF/DOCX/PPTX referenced by the course HTML
    into a new accessible wiki page and rewrite the source <a href> links
    to point at the new page. Requires the LiteParse Node CLI on PATH
    and a reachable Ollama endpoint with the configured text model.
    Heavy — a single PDF can take 10s–minutes. Document conversion runs
    BEFORE the regular per-page remediation so the original pages get
    analyzed with the rewritten hrefs."""

    generate_alt_text: bool = False
    """Fill empty/missing alt attributes on bundled images by running the
    configured Ollama vision model against the image bytes extracted from
    the IMSCC archive. Only images that (a) actually exist as zip members
    and (b) triggered an IMG001 issue on their page are described — the
    pipeline skips external http(s) image URLs silently.

    Tradeoff: adds roughly 5–30 seconds per unique image on a CPU-only
    machine. Requires Ollama to be reachable and the configured vision
    model to be installed. When disabled (the default), IMG001 is left to
    the transformer's deterministic fallback (filename-derived alt)."""


@dataclass
class PageReport:
    """Per-page outcome inside a RemediationSummary."""

    identifier: str
    title: str
    content_type: str
    file_path: str
    issues_before: int
    issues_after: int
    body_issue_rule_ids: list[str] = field(default_factory=list)
    rendered_issue_rule_ids: list[str] = field(default_factory=list)
    skipped_reason: str | None = None


@dataclass
class RemediationSummary:
    """Aggregate result of `remediate_course`."""

    job_id: str
    input_path: str
    output_path: str
    course_title: str
    page_count: int
    pages_modified: int
    issues_before: int
    issues_after: int
    pages: list[PageReport] = field(default_factory=list)
    documents_converted: int = 0
    documents_skipped: int = 0
    alt_texts_generated: int = 0
    suggestions: list[FixSuggestion] = field(default_factory=list)


async def remediate_course(
    input_path: str | Path,
    output_path: str | Path,
    *,
    job_id: str = "job",
    options: RemediationOptions = RemediationOptions(),
    on_progress: ProgressCallback | None = None,
) -> RemediationSummary:
    """Run the full remediation pipeline on an IMSCC archive.

    The output archive is byte-equivalent to the input except for the
    HTML-bearing pages that got modified — passthrough files (images,
    PDFs, course_settings/*, manifest, quiz QTI XML, etc.) stay
    byte-identical.
    """

    def emit(phase: str, **payload: Any) -> None:
        if on_progress is None:
            return
        on_progress({"job_id": job_id, "phase": phase, **payload})

    analyzer = AccessibilityAnalyzer()
    transformer = HTMLTransformer()
    validator = CanvasHTMLValidator()

    emit("parse.start", input_path=str(input_path))
    parsed = parse(input_path)
    emit(
        "parse.done",
        input_path=str(input_path),
        course_title=parsed.course_title,
        page_count=len(parsed.pages),
    )

    # ---- Document conversion (optional) ---------------------------------
    doc_result: DocumentConversionResult | None = None
    if options.include_document_conversion:
        doc_converter = _load_default_document_converter()
        emit("docs.start")
        doc_result = await convert_and_rewrite(
            parsed, converter=doc_converter, on_progress=on_progress
        )
        emit(
            "docs.done",
            converted=len(doc_result.converted),
            skipped=len(doc_result.skipped),
        )
        # Rewritten pages replace the originals in `parsed.pages` so the
        # body scan / transformer sees the new link hrefs. Pydantic copies
        # in convert_and_rewrite keep the originals untouched.
        if doc_result.rewritten_pages:
            rewritten_by_id = {p.identifier: p for p in doc_result.rewritten_pages}
            parsed = _replace_pages(parsed, rewritten_by_id)

    total = len(parsed.pages)
    reports: list[PageReport] = []
    patched_pages: list[CoursePage] = []
    # Per-page residual issues + the final HTML shipped for that page
    # (patched for writable modified pages, original otherwise). Used as
    # the source of truth for suggestion generation after the main loop.
    residuals_by_page: list[tuple[CoursePage, str, list[AccessibilityIssue]]] = []
    total_before = 0
    total_after = 0
    alt_texts_generated = 0

    rendered_scan_async = _maybe_load_rendered_scan(
        enabled=options.include_rendered_scan
    )

    # Bind an image fetcher to the (possibly post-doc-conversion) parsed
    # archive — the fetcher reads from the on-disk IMSCC so doc conversion
    # doesn't invalidate it. Only constructed when alt-text gen is enabled
    # so default runs don't pay for the zip member inventory.
    # Always construct — the suggestion pass also needs it for IMG001
    # alt-text proposals, not just the opt-in alt-text auto-fill path.
    image_fetcher: IMSCCImageFetcher = IMSCCImageFetcher(parsed)

    for idx, page in enumerate(parsed.pages, start=1):
        emit(
            "page.start",
            index=idx,
            total=total,
            identifier=page.identifier,
            title=page.title,
            content_type=page.content_type.value,
        )

        # ---- Scan (body + optional rendered) -----------------------------
        body_issues = await analyzer.analyze_page(page)
        rendered_issues: list[AccessibilityIssue] = []
        if rendered_scan_async is not None and page.content_type != ContentType.QUIZ:
            try:
                rendered_issues = await rendered_scan_async(
                    page.html_content, page_id=page.identifier, title=page.title
                )
            except Exception as exc:  # noqa: BLE001 — keep going on one bad page
                emit(
                    "page.render_error",
                    index=idx,
                    identifier=page.identifier,
                    error=f"{type(exc).__name__}: {exc}",
                )

        issues_before = len(body_issues) + len(rendered_issues)
        total_before += issues_before

        body_rule_ids = sorted({i.rule_id for i in body_issues})
        rendered_rule_ids = sorted({i.rule_id for i in rendered_issues})

        # ---- Decide whether to patch ------------------------------------
        writable = (
            page.content_type in _WRITABLE_TYPES
            and (page.content_type != ContentType.QUIZ or options.include_quiz_pages)
        )
        skipped_reason: str | None = None

        if not writable:
            skipped_reason = f"content_type {page.content_type.value} is parse-only"

        # ---- AI alt-text generation (optional) --------------------------
        # Run before the transformer so the generated alts flow in via the
        # transformer's existing `alt_texts=` kwarg. Only attempted on
        # writable pages that have IMG001 issues AND the feature is opted
        # into. External image URLs and un-resolvable zip refs are silently
        # skipped per-image.
        generated_alts: dict[str, str] = {}
        if options.generate_alt_text and writable:
            img001_srcs = _collect_img001_srcs(body_issues, page.html_content)
            if img001_srcs:
                emit(
                    "page.alt_text.start",
                    index=idx,
                    identifier=page.identifier,
                    image_count=len(img001_srcs),
                )
                generated_alts = await _generate_alt_texts_for_page(
                    srcs=img001_srcs,
                    fetcher=image_fetcher,
                    page=page,
                    on_progress=emit,
                    page_index=idx,
                )
                alt_texts_generated += len(generated_alts)

        # ---- Transform + validate ---------------------------------------
        issues_after = issues_before
        modified = False
        residual_issues: list[AccessibilityIssue] = list(body_issues)
        final_html = page.html_content
        if writable and body_issues:
            try:
                new_html = transformer.transform(
                    page.html_content,
                    body_issues,
                    alt_texts=generated_alts or None,
                )
                if options.sanitize_after_transform:
                    # CanvasHTMLValidator.sanitize returns (html, ValidationResult)
                    new_html, _validation = validator.sanitize(new_html)
                if new_html != page.html_content:
                    modified = True
                    patched = page.model_copy(update={"html_content": new_html})
                    patched_pages.append(patched)
                    final_html = new_html
                    # Re-scan the transformed HTML to report what's left.
                    rescan = await analyzer.analyze_page(patched)
                    residual_issues = list(rescan)
                    issues_after = len(rescan) + len(rendered_issues)
                else:
                    issues_after = issues_before
            except Exception as exc:  # noqa: BLE001
                skipped_reason = f"transform failed: {type(exc).__name__}: {exc}"
                emit(
                    "page.transform_error",
                    index=idx,
                    identifier=page.identifier,
                    error=skipped_reason,
                )

        residuals_by_page.append((page, final_html, residual_issues))
        total_after += issues_after

        reports.append(
            PageReport(
                identifier=page.identifier,
                title=page.title,
                content_type=page.content_type.value,
                file_path=page.file_path,
                issues_before=issues_before,
                issues_after=issues_after,
                body_issue_rule_ids=body_rule_ids,
                rendered_issue_rule_ids=rendered_rule_ids,
                skipped_reason=skipped_reason,
            )
        )

        emit(
            "page.done",
            index=idx,
            total=total,
            identifier=page.identifier,
            issues_before=issues_before,
            issues_after=issues_after,
            modified=modified,
        )

    # ---- Write the output archive ---------------------------------------
    output = Path(output_path)
    new_files = doc_result.new_files if doc_result else None
    manifest_override = doc_result.manifest_bytes if doc_result else None
    emit(
        "build.start",
        output_path=str(output),
        modified_pages=len(patched_pages),
        new_files=len(new_files or {}),
    )
    build(
        parsed,
        patched_pages,
        output,
        new_files=new_files,
        manifest_override=manifest_override,
    )
    emit("build.done", output_path=str(output))

    # ---- Suggestion pass (LNK004 + IMG001) ------------------------------
    suggestions = await _generate_suggestions(
        residuals_by_page=residuals_by_page,
        image_fetcher=image_fetcher,
        on_progress=emit,
    )

    summary = RemediationSummary(
        job_id=job_id,
        input_path=str(input_path),
        output_path=str(output),
        course_title=parsed.course_title,
        page_count=total,
        pages_modified=len(patched_pages),
        issues_before=total_before,
        issues_after=total_after,
        pages=reports,
        documents_converted=len(doc_result.converted) if doc_result else 0,
        documents_skipped=len(doc_result.skipped) if doc_result else 0,
        alt_texts_generated=alt_texts_generated,
        suggestions=suggestions,
    )
    emit(
        "completed",
        page_count=summary.page_count,
        pages_modified=summary.pages_modified,
        issues_before=summary.issues_before,
        issues_after=summary.issues_after,
    )
    return summary


def _maybe_load_rendered_scan(*, enabled: bool):
    """Lazy-import rendered_scan_async so the `render` extra stays optional."""
    if not enabled:
        return None
    try:
        from crd_sidecar.render import rendered_scan_async

        return rendered_scan_async
    except ImportError as exc:
        raise RuntimeError(
            "include_rendered_scan=True but the `render` extra is not "
            f"installed ({exc}). Run: uv sync --extra render"
        ) from exc


def _load_default_document_converter():
    """Production converter: LiteParse + LLM through the vendored crd_core."""
    from crd_sidecar.crd_core.documents import convert_document_to_html

    async def _convert(file_path: str, title: str) -> str:
        return await convert_document_to_html(file_path, title=title)

    return _convert


def _collect_img001_srcs(
    issues: list[AccessibilityIssue], page_html: str
) -> list[str]:
    """Extract the unique `<img src>` values from IMG001 issues on a page.

    IMG001 stores the offending `<img ...>` fragment in `element_html`; we
    parse that fragment to pull the `src` attribute. A single image might
    be flagged more than once across rules — we dedup while preserving the
    document order from the page HTML so the emitted events feel intuitive.
    """
    img_tags: list[str] = []
    for issue in issues:
        if issue.rule_id != "IMG001":
            continue
        if not issue.element_html:
            continue
        img_tags.append(issue.element_html)

    if not img_tags:
        return []

    # Parse each fragment individually — element_html values are single-tag
    # snippets, so BeautifulSoup parses them reliably.
    seen: set[str] = set()
    ordered: list[str] = []
    for fragment in img_tags:
        soup = BeautifulSoup(fragment, "html.parser")
        img = soup.find("img")
        if img is None:
            continue
        src = (img.get("src") or "").strip()
        if not src or src in seen:
            continue
        seen.add(src)
        ordered.append(src)

    # Fall back to scanning the page HTML for document-order stability when
    # the issues carry full snippets (the order above is driven by issue
    # order, which should already match document order for images).
    if not ordered:
        soup = BeautifulSoup(page_html, "html.parser")
        for img in soup.find_all("img"):
            src = (img.get("src") or "").strip()
            if src and src not in seen:
                seen.add(src)
                ordered.append(src)
    return ordered


async def _generate_alt_texts_for_page(
    *,
    srcs: list[str],
    fetcher: IMSCCImageFetcher,
    page: CoursePage,
    on_progress: Callable[..., None],
    page_index: int,
) -> dict[str, str]:
    """For each src that resolves to a bundled zip member, call the vision
    client and collect `{src → alt_text}`. External URLs and unresolvable
    paths are skipped silently. One failure does not abort the page — it
    just emits a `page.alt_text.error` event and moves on.
    """
    out: dict[str, str] = {}
    # One temp dir per page keeps extracted images scoped — cleaned up on
    # exit. Using TemporaryDirectory so crashes don't leak /tmp entries.
    with tempfile.TemporaryDirectory(prefix="crd-alt-") as tmp_root:
        for src in srcs:
            zip_path = fetcher.resolve_src_to_zip_path(src)
            if zip_path is None:
                # External or unresolvable — per design, silent skip.
                continue
            try:
                extracted = fetcher.extract_to(src, tmp_root)
                if extracted is None:
                    continue
                alt = await alt_text_module.generate_alt_text_for_file(
                    extracted,
                    context=page.title or "",
                )
            except Exception as exc:  # noqa: BLE001
                on_progress(
                    "page.alt_text.error",
                    index=page_index,
                    identifier=page.identifier,
                    src=src,
                    error=f"{type(exc).__name__}: {exc}",
                )
                continue
            if not alt:
                continue
            out[src] = alt
            on_progress(
                "page.alt_text.generated",
                index=page_index,
                identifier=page.identifier,
                src=src,
                alt_length=len(alt),
            )
    return out


async def _generate_suggestions(
    *,
    residuals_by_page: list[tuple[CoursePage, str, list[AccessibilityIssue]]],
    image_fetcher: IMSCCImageFetcher,
    on_progress: Callable[..., None],
) -> list[FixSuggestion]:
    """Produce LLM-assisted fix suggestions for residual judgment-call
    issues across all supported categories.

    Currently handles:
    - LNK004 (non-descriptive link text) via the text model
    - IMG001 (missing alt text) via the vision model

    Fails open — LLM unavailability, missing images, and per-item
    errors all return None/skip rather than blocking the remediation
    output.
    """
    link_targets: list[tuple[CoursePage, str, AccessibilityIssue]] = []
    img_targets: list[tuple[CoursePage, str, AccessibilityIssue]] = []
    form_targets: list[tuple[CoursePage, str, AccessibilityIssue]] = []
    table_targets: list[tuple[CoursePage, str, AccessibilityIssue]] = []
    contrast_targets: list[tuple[CoursePage, str, AccessibilityIssue]] = []
    for page, final_html, residuals in residuals_by_page:
        for issue in residuals:
            if issue.rule_id == "LNK004":
                link_targets.append((page, final_html, issue))
            elif issue.rule_id == "IMG001":
                img_targets.append((page, final_html, issue))
            elif issue.rule_id == "FORM003":
                form_targets.append((page, final_html, issue))
            elif issue.rule_id == "TBL003":
                table_targets.append((page, final_html, issue))
            elif issue.rule_id == "CLR001" and issue.axe_meta:
                contrast_targets.append((page, final_html, issue))

    total_targets = (
        len(link_targets)
        + len(img_targets)
        + len(form_targets)
        + len(table_targets)
        + len(contrast_targets)
    )
    if total_targets == 0:
        return []

    try:
        from crd_sidecar.crd_core.ai.vision_client import OllamaVisionClient

        client = OllamaVisionClient()
    except Exception:  # noqa: BLE001
        return []

    on_progress(
        "suggestions.start",
        count=total_targets,
        link_count=len(link_targets),
        image_count=len(img_targets),
        form_count=len(form_targets),
        table_count=len(table_targets),
        contrast_count=len(contrast_targets),
    )
    suggestions: list[FixSuggestion] = []
    done = 0

    # --- CLR001 pass (pure color math, no LLM) ---
    for page, final_html, issue in contrast_targets:
        try:
            suggestion = generate_contrast_suggestion(
                issue,
                final_html,
                page_title=page.title,
                page_file_path=page.file_path,
            )
        except Exception:  # noqa: BLE001
            suggestion = None
        if suggestion is not None:
            suggestions.append(suggestion)
        done += 1
        on_progress(
            "suggestions.progress",
            index=done,
            total=total_targets,
            generated=len(suggestions),
            kind="contrast",
        )

    # --- LNK004 pass (text model) ---
    for page, final_html, issue in link_targets:
        try:
            suggestion = await generate_link_text_suggestion(
                issue,
                final_html,
                client,
                page_title=page.title,
                page_file_path=page.file_path,
            )
        except Exception:  # noqa: BLE001
            suggestion = None
        if suggestion is not None:
            suggestions.append(suggestion)
        done += 1
        on_progress(
            "suggestions.progress",
            index=done,
            total=total_targets,
            generated=len(suggestions),
            kind="link_text",
        )

    # --- TBL003 pass (text model, same shape as link_text) ---
    for page, final_html, issue in table_targets:
        try:
            suggestion = await generate_table_caption_suggestion(
                issue,
                final_html,
                client,
                page_title=page.title,
                page_file_path=page.file_path,
            )
        except Exception:  # noqa: BLE001
            suggestion = None
        if suggestion is not None:
            suggestions.append(suggestion)
        done += 1
        on_progress(
            "suggestions.progress",
            index=done,
            total=total_targets,
            generated=len(suggestions),
            kind="table_caption",
        )

    # --- FORM003 pass (text model, same shape as link_text) ---
    for page, final_html, issue in form_targets:
        try:
            suggestion = await generate_form_label_suggestion(
                issue,
                final_html,
                client,
                page_title=page.title,
                page_file_path=page.file_path,
            )
        except Exception:  # noqa: BLE001
            suggestion = None
        if suggestion is not None:
            suggestions.append(suggestion)
        done += 1
        on_progress(
            "suggestions.progress",
            index=done,
            total=total_targets,
            generated=len(suggestions),
            kind="form_label",
        )

    # --- IMG001 pass (vision model, scoped to one temp dir) ---
    if img_targets:
        with tempfile.TemporaryDirectory(prefix="crd-alt-suggest-") as tmp_root:
            for page, final_html, issue in img_targets:
                try:
                    suggestion = await generate_alt_text_suggestion(
                        issue,
                        final_html,
                        image_fetcher,
                        tmp_root=tmp_root,
                        page_title=page.title,
                        page_file_path=page.file_path,
                    )
                except Exception:  # noqa: BLE001
                    suggestion = None
                if suggestion is not None:
                    suggestions.append(suggestion)
                done += 1
                on_progress(
                    "suggestions.progress",
                    index=done,
                    total=total_targets,
                    generated=len(suggestions),
                    kind="alt_text",
                )

    on_progress("suggestions.done", generated=len(suggestions))
    return suggestions


def _replace_pages(
    parsed: ParsedIMSCC, rewritten_by_id: dict[str, CoursePage]
) -> ParsedIMSCC:
    """Return a new ParsedIMSCC whose pages list reflects the rewritten
    versions for any identifier in `rewritten_by_id`. Other pages are kept.
    """
    new_pages = [
        rewritten_by_id.get(p.identifier, p) for p in parsed.pages
    ]
    # ParsedIMSCC is a plain dataclass; construct a copy with new pages.
    return ParsedIMSCC(
        path=parsed.path,
        course_title=parsed.course_title,
        schema_version=parsed.schema_version,
        pages=new_pages,
        resources=parsed.resources,
        manifest_bytes=parsed.manifest_bytes,
    )
