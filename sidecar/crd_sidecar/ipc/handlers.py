"""Built-in RPC handlers."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from crd_sidecar import __version__
from crd_sidecar.crd_core.accessibility.analyzer import AccessibilityAnalyzer
from crd_sidecar.crd_core.config import get_settings
from crd_sidecar.crd_core.models import ContentType, CoursePage
from crd_sidecar.imscc import parse as parse_imscc_archive
from crd_sidecar.ipc.rpc import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    Dispatcher,
    RpcError,
    get_current_emitter,
)

# Analyzer registration is expensive (65 rules). Build once per process.
_analyzer: AccessibilityAnalyzer | None = None


def _get_analyzer() -> AccessibilityAnalyzer:
    global _analyzer
    if _analyzer is None:
        _analyzer = AccessibilityAnalyzer()
    return _analyzer


def ping(_params: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "service": "remedy-canvas-desktop-sidecar"}


def version(_params: dict[str, Any]) -> dict[str, Any]:
    return {"version": __version__}


def analyze_html(params: dict[str, Any]) -> dict[str, Any]:
    """Run the 65-rule accessibility analyzer against an HTML fragment.

    Params:
        html (str, required): HTML content to analyze.
        title (str, optional): Page title. Defaults to "Untitled".
        identifier (str, optional): Stable page identifier. Defaults to "adhoc".
        content_type (str, optional): One of ContentType enum values.
            Defaults to "WIKI_PAGE".
    """
    html = params.get("html")
    if not isinstance(html, str):
        raise RpcError(INVALID_PARAMS, "params.html must be a string")

    title = params.get("title") or "Untitled"
    identifier = params.get("identifier") or "adhoc"
    content_type_raw = params.get("content_type") or ContentType.WIKI_PAGE.value
    try:
        content_type = ContentType(content_type_raw)
    except ValueError as exc:
        raise RpcError(INVALID_PARAMS, f"Invalid content_type: {exc}") from exc

    page = CoursePage(
        id=identifier,
        identifier=identifier,
        title=title,
        content_type=content_type,
        html_content=html,
    )

    analyzer = _get_analyzer()
    issues = asyncio.run(analyzer.analyze_page(page))

    return {
        "rule_count": len(analyzer.rules),
        "issue_count": len(issues),
        "issues": [issue.model_dump(mode="json") for issue in issues],
    }


def _require_existing_path(params: dict[str, Any], key: str) -> Path:
    raw = params.get(key)
    if not isinstance(raw, str) or not raw:
        raise RpcError(INVALID_PARAMS, f"params.{key} must be a non-empty string path")
    path = Path(raw).expanduser()
    if not path.is_file():
        raise RpcError(INVALID_PARAMS, f"params.{key} is not a readable file: {path}")
    return path


def parse_imscc(params: dict[str, Any]) -> dict[str, Any]:
    """Parse an IMSCC archive. Returns an inventory without running the analyzer."""
    path = _require_existing_path(params, "path")
    parsed = parse_imscc_archive(path)
    return {
        "path": str(parsed.path),
        "course_title": parsed.course_title,
        "schema_version": parsed.schema_version,
        "page_count": len(parsed.pages),
        "resource_count": len(parsed.resources),
        "pages": [
            {
                "identifier": p.identifier,
                "title": p.title,
                "content_type": p.content_type.value,
                "file_path": p.file_path,
                "html_size": len(p.html_content),
            }
            for p in parsed.pages
        ],
    }


def analyze_imscc(params: dict[str, Any]) -> dict[str, Any]:
    """Parse an IMSCC and run the 65-rule analyzer across every HTML page.

    Returns per-page issue counts + a total. Full issue detail stays off
    this response by default to keep the payload small for UI rendering; a
    follow-up page-level `analyze_html` call can pull the full list.
    """
    path = _require_existing_path(params, "path")
    include_issues = bool(params.get("include_issues", False))

    parsed = parse_imscc_archive(path)
    analyzer = _get_analyzer()

    page_reports: list[dict[str, Any]] = []
    total_issues = 0

    async def _run() -> None:
        nonlocal total_issues
        for page in parsed.pages:
            issues = await analyzer.analyze_page(page)
            total_issues += len(issues)
            report: dict[str, Any] = {
                "identifier": page.identifier,
                "title": page.title,
                "file_path": page.file_path,
                "issue_count": len(issues),
            }
            if include_issues:
                report["issues"] = [issue.model_dump(mode="json") for issue in issues]
            page_reports.append(report)

    asyncio.run(_run())

    return {
        "path": str(parsed.path),
        "course_title": parsed.course_title,
        "page_count": len(parsed.pages),
        "total_issues": total_issues,
        "pages": page_reports,
    }


def rendered_scan(params: dict[str, Any]) -> dict[str, Any]:
    """Render the given HTML in a mock-Canvas shell and run axe-core.

    Catches the CSS-contrast / focus / target-size issues the body-only
    analyzer misses — the standalone-app replacement for Canvas Remedy-LTI's
    Canvas-live rendered_scanner.

    Params:
        html (str, required): HTML body to render.
        title (str, optional): Page title for the mock shell. Default "Canvas Page".
        page_id (str, optional): Identifier propagated onto each issue.
    """
    html = params.get("html")
    if not isinstance(html, str):
        raise RpcError(INVALID_PARAMS, "params.html must be a string")

    title = params.get("title") or "Canvas Page"
    page_id = params.get("page_id") or "adhoc"

    # Import lazily — the render module requires playwright, which isn't in
    # the default dependency set.
    try:
        from crd_sidecar.render import rendered_scan_async
    except ImportError as exc:
        raise RpcError(
            INTERNAL_ERROR,
            f"Render extra not installed: {exc}. Run `uv sync --extra render`.",
        ) from exc

    issues = asyncio.run(
        rendered_scan_async(html, page_id=page_id, title=title)
    )
    return {
        "page_id": page_id,
        "issue_count": len(issues),
        "issues": [issue.model_dump(mode="json") for issue in issues],
    }


def ai_status(_params: dict[str, Any]) -> dict[str, Any]:
    """Check that the configured Ollama endpoint is reachable and report
    whether the configured text/vision models are present locally.
    """
    import json as _json
    import urllib.error
    import urllib.request

    settings = get_settings()
    # /api/tags lives on the native Ollama API root, NOT under /v1
    base = settings.ollama_base_url.rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    url = f"{base}/api/tags"

    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = _json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        return {
            "reachable": False,
            "base_url": settings.ollama_base_url,
            "error": f"{type(exc).__name__}: {exc}",
        }

    installed = [m["name"] for m in data.get("models", [])]
    return {
        "reachable": True,
        "base_url": settings.ollama_base_url,
        "text_model": settings.ollama_text_model,
        "vision_model": settings.ollama_vision_model,
        "provider": settings.provider.value,
        "resolved_base_url": settings.resolved_base_url(),
        "resolved_text_model": settings.resolved_text_model(),
        "resolved_vision_model": settings.resolved_vision_model(),
        "text_model_installed": settings.ollama_text_model in installed,
        "vision_model_installed": settings.ollama_vision_model in installed,
        "installed_models": installed,
    }


def ai_generate_alt_text(params: dict[str, Any]) -> dict[str, Any]:
    """Run the configured vision model on a local image file and return alt text.

    Params:
        image_path (str, required): path to a local image file
        context (str, optional): surrounding text for the model
        model (str, optional): override the default vision model
    """
    from crd_sidecar.crd_core.ai.alt_text import generate_alt_text_for_file

    image_path = params.get("image_path")
    if not isinstance(image_path, str) or not image_path:
        raise RpcError(INVALID_PARAMS, "params.image_path must be a non-empty string")

    context = params.get("context") or ""
    model = params.get("model")

    try:
        alt = asyncio.run(
            generate_alt_text_for_file(image_path, context=context, model=model)
        )
    except FileNotFoundError as exc:
        raise RpcError(INVALID_PARAMS, str(exc)) from exc

    return {"alt_text": alt, "model": model or get_settings().ollama_vision_model}


def remediate_imscc(params: dict[str, Any]) -> dict[str, Any]:
    """Run the full remediation pipeline on an IMSCC archive.

    Emits `job.progress` notifications (JSON-RPC 2.0 requests without an id)
    over stdout as each pipeline phase / page advances. The final response
    carries the full RemediationSummary.

    Params:
        input_path (str, required)   — path to the source IMSCC
        output_path (str, required)  — where to write the remediated IMSCC
        job_id (str, optional)       — correlation id embedded in every event
        options (object, optional)   — RemediationOptions toggles:
            include_rendered_scan (bool, default false)
            include_quiz_pages (bool, default false)
            sanitize_after_transform (bool, default true)
            include_document_conversion (bool, default false)
            generate_alt_text (bool, default false) — fill missing alt via
                the Ollama vision model for bundled images only.
    """
    from crd_sidecar.orchestrator import (
        RemediationOptions,
        remediate_course,
    )

    input_path = _require_existing_path(params, "input_path")
    raw_output = params.get("output_path")
    if not isinstance(raw_output, str) or not raw_output:
        raise RpcError(INVALID_PARAMS, "params.output_path must be a non-empty string")
    from pathlib import Path as _Path

    output_path = _Path(raw_output).expanduser()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    job_id = params.get("job_id") or "job"

    opts_in = params.get("options") or {}
    if not isinstance(opts_in, dict):
        raise RpcError(INVALID_PARAMS, "params.options must be an object if provided")
    try:
        options = RemediationOptions(
            include_rendered_scan=bool(opts_in.get("include_rendered_scan", False)),
            include_quiz_pages=bool(opts_in.get("include_quiz_pages", False)),
            sanitize_after_transform=bool(
                opts_in.get("sanitize_after_transform", True)
            ),
            include_document_conversion=bool(
                opts_in.get("include_document_conversion", False)
            ),
            generate_alt_text=bool(opts_in.get("generate_alt_text", False)),
        )
    except TypeError as exc:
        raise RpcError(INVALID_PARAMS, f"invalid options: {exc}") from exc

    emitter = get_current_emitter()

    def on_progress(event: dict[str, Any]) -> None:
        if emitter is not None:
            emitter.emit("job.progress", event)

    summary = asyncio.run(
        remediate_course(
            input_path=input_path,
            output_path=output_path,
            job_id=job_id,
            options=options,
            on_progress=on_progress,
        )
    )

    return {
        "job_id": summary.job_id,
        "input_path": summary.input_path,
        "output_path": summary.output_path,
        "course_title": summary.course_title,
        "page_count": summary.page_count,
        "pages_modified": summary.pages_modified,
        "issues_before": summary.issues_before,
        "issues_after": summary.issues_after,
        "documents_converted": summary.documents_converted,
        "documents_skipped": summary.documents_skipped,
        "alt_texts_generated": summary.alt_texts_generated,
        "pages": [
            {
                "identifier": p.identifier,
                "title": p.title,
                "content_type": p.content_type,
                "file_path": p.file_path,
                "issues_before": p.issues_before,
                "issues_after": p.issues_after,
                "body_issue_rule_ids": p.body_issue_rule_ids,
                "rendered_issue_rule_ids": p.rendered_issue_rule_ids,
                "skipped_reason": p.skipped_reason,
            }
            for p in summary.pages
        ],
        "suggestions": [s.model_dump() for s in summary.suggestions],
    }


def convert_document(params: dict[str, Any]) -> dict[str, Any]:
    """Convert a local PDF/DOCX/PPTX file to accessible HTML via LiteParse + LLM.

    Params:
        file_path (str, required): local path to the document
        title (str, optional): document title to seed the LLM prompt
    """
    # Validate params BEFORE importing the heavy optional extra, so a missing
    # file returns INVALID_PARAMS even when liteparse isn't installed.
    file_path = _require_existing_path(params, "file_path")
    title = params.get("title") or ""

    try:
        from crd_sidecar.crd_core.documents import convert_document_to_html
    except ImportError as exc:
        raise RpcError(
            INTERNAL_ERROR,
            f"Documents extra not installed: {exc}. "
            "Run `uv sync --extra documents` (dev) or wait for Phase 7b to bundle LiteParse.",
        ) from exc

    html = asyncio.run(convert_document_to_html(file_path, title=title))
    return {
        "file_path": str(file_path),
        "title": title or file_path.stem,
        "html": html,
        "html_size": len(html),
    }


def export_acr(params: dict[str, Any]) -> dict[str, Any]:
    """Generate an Accessibility Conformance Report (ACR) for an IMSCC.

    Parses the IMSCC, runs the 66-rule analyzer across every HTML page (and
    optionally the rendered axe-core scanner), builds a VPAT 2.5-style
    ``CourseACR``, and writes the selected export format to disk.

    Params:
        input_path (str, required): path to an IMSCC archive
        output_path (str, required): path to write the rendered report
        format (str, required): "html" | "markdown" | "json"
        course_name (str, optional): defaults to parsed course title
        course_url (str, optional): hyperlink shown in the report header
        evaluator (str, optional): defaults to "Remedy Canvas Desktop"
        include_rendered_scan (bool, optional, default false): also run the
            axe-core rendered scanner page-by-page and merge those issues.
            Quiz pages are skipped — they contain QTI-wrapped markup that
            renders garbage in a generic Canvas shell.
    """
    from crd_sidecar.crd_core.services.acr_export_service import (
        ACRExportService,
    )
    from crd_sidecar.crd_core.services.acr_service import build_course_acr

    input_path = _require_existing_path(params, "input_path")
    raw_output = params.get("output_path")
    if not isinstance(raw_output, str) or not raw_output:
        raise RpcError(INVALID_PARAMS, "params.output_path must be a non-empty string")
    output_path = Path(raw_output).expanduser()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    fmt = (params.get("format") or "html").lower()
    if fmt not in {"html", "markdown", "json"}:
        raise RpcError(
            INVALID_PARAMS,
            f"params.format must be one of html|markdown|json, got {fmt!r}",
        )

    include_rendered = bool(params.get("include_rendered_scan", False))
    course_url = params.get("course_url") or ""
    evaluator = params.get("evaluator") or "Remedy Canvas Desktop"

    parsed = parse_imscc_archive(input_path)
    course_name = params.get("course_name") or parsed.course_title or input_path.stem

    analyzer = _get_analyzer()

    async def _run() -> Any:
        report = await analyzer.analyze_course(
            parsed.pages, course_id=parsed.course_title or input_path.stem
        )

        if include_rendered:
            # Merge rendered-scan issues in. Lazy-import so the render extra
            # stays optional — skip silently if playwright isn't installed.
            try:
                from crd_sidecar.render import rendered_scan_async
            except ImportError:
                rendered_scan_async = None  # type: ignore[assignment]

            if rendered_scan_async is not None:
                for page in parsed.pages:
                    # Quiz pages hold QTI wrappers, not renderable HTML. Skip.
                    if page.content_type == ContentType.QUIZ:
                        continue
                    try:
                        rendered_issues = await rendered_scan_async(
                            page.html_content,
                            page_id=page.id,
                            title=page.title,
                        )
                    except Exception:  # noqa: BLE001
                        # Best-effort — a single page failing the rendered
                        # scan must not abort the whole ACR.
                        continue
                    for issue in rendered_issues:
                        issue.page_identifier = page.identifier
                    report.issues.extend(rendered_issues)

                # Re-tally counts after the merge so the exported ACR
                # reflects the combined body + rendered totals.
                report.total_issues = len(report.issues)
                report.errors = sum(
                    1 for i in report.issues if i.severity.value == "error"
                )
                report.warnings = sum(
                    1 for i in report.issues if i.severity.value == "warning"
                )
                report.info = sum(
                    1 for i in report.issues if i.severity.value == "info"
                )

        return report

    report = asyncio.run(_run())

    acr = build_course_acr(
        report,
        course_id=parsed.course_title or input_path.stem,
        course_name=course_name,
        course_url=course_url,
        evaluator=evaluator,
    )

    exporter = ACRExportService()
    if fmt == "html":
        rendered = exporter.export_html(acr)
    elif fmt == "markdown":
        rendered = exporter.export_markdown(acr)
    else:  # json
        rendered = exporter.export_json(acr)

    output_path.write_text(rendered, encoding="utf-8")

    criterion_counts = {
        "supports": sum(
            1 for c in acr.criteria
            if c.conformance.value == "Supports"
        ),
        "partially_supports": sum(
            1 for c in acr.criteria
            if c.conformance.value == "Partially Supports"
        ),
        "does_not_support": sum(
            1 for c in acr.criteria
            if c.conformance.value == "Does Not Support"
        ),
        "not_applicable": sum(
            1 for c in acr.criteria
            if c.conformance.value == "Not Applicable"
        ),
    }

    return {
        "output_path": str(output_path),
        "format": fmt,
        "conformance_percentage": acr.conformance_percentage,
        "overall_status": acr.overall_status.value,
        "total_issues": report.total_issues,
        "pages_analyzed": report.pages_analyzed,
        "criterion_counts": criterion_counts,
        "course_name": course_name,
    }


def inventory_documents(params: dict[str, Any]) -> dict[str, Any]:
    """List every PDF/DOCX/PPTX referenced by the HTML pages in an IMSCC,
    deduplicated per unique target. Phase 5.5b will feed this inventory
    into the conversion + link-rewriting pipeline.
    """
    from crd_sidecar.imscc import discover_document_links

    path = _require_existing_path(params, "path")
    parsed = parse_imscc_archive(path)
    links = discover_document_links(parsed)

    return {
        "path": str(parsed.path),
        "course_title": parsed.course_title,
        "bundled_count": sum(1 for link in links if not link.is_external),
        "external_count": sum(1 for link in links if link.is_external),
        "documents": [
            {
                "resolved_path": link.resolved_path,
                "is_external": link.is_external,
                "extension": link.extension,
                "display_text": link.display_text,
                "source_page_ids": link.source_page_ids,
                "original_hrefs": link.original_hrefs,
            }
            for link in links
        ],
    }


def apply_suggestions(params: dict[str, Any]) -> dict[str, Any]:
    """Apply a list of accepted FixSuggestion objects to an IMSCC.

    The input IMSCC is the previously-remediated archive. For each
    accepted suggestion, locate the target page by file_path, rewrite
    the affected element (dispatched by rule_id), and run the Canvas
    sanitizer before writing to output_path.

    Suggestions whose original snippet can no longer be located (page
    changed externally, or two suggestions overlap) are counted under
    `not_applied` but do not fail the batch.

    Params:
        input_path (str, required): path to the remediated IMSCC
        output_path (str, required): path to write the new IMSCC
        suggestions (list, required): accepted FixSuggestion dicts
    """
    from crd_sidecar.crd_core.remediation.canvas_validator import (
        CanvasHTMLValidator,
    )
    from crd_sidecar.crd_core.suggestions import (
        FixSuggestion,
        apply_suggestion_to_page,
    )
    from crd_sidecar.imscc import build as build_imscc

    input_path = _require_existing_path(params, "input_path")
    raw_output = params.get("output_path")
    if not isinstance(raw_output, str) or not raw_output:
        raise RpcError(INVALID_PARAMS, "params.output_path must be a non-empty string")
    output_path = Path(raw_output).expanduser()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    raw_suggestions = params.get("suggestions")
    if not isinstance(raw_suggestions, list):
        raise RpcError(INVALID_PARAMS, "params.suggestions must be a list")

    suggestions: list[FixSuggestion] = []
    for i, raw in enumerate(raw_suggestions):
        if not isinstance(raw, dict):
            raise RpcError(
                INVALID_PARAMS, f"params.suggestions[{i}] must be an object"
            )
        try:
            suggestions.append(FixSuggestion.model_validate(raw))
        except Exception as exc:  # noqa: BLE001
            raise RpcError(
                INVALID_PARAMS,
                f"params.suggestions[{i}] is not a valid FixSuggestion: {exc}",
            ) from exc

    parsed = parse_imscc_archive(input_path)
    validator = CanvasHTMLValidator()

    # Group by page_file_path so multiple suggestions against one page
    # apply to a single accumulating HTML buffer.
    by_page: dict[str, list[FixSuggestion]] = {}
    for s in suggestions:
        key = s.page_file_path or s.page_id
        if not key:
            continue
        by_page.setdefault(key, []).append(s)

    applied = 0
    not_applied: list[dict[str, str]] = []
    patched_pages: list[CoursePage] = []

    for page in parsed.pages:
        page_key = page.file_path or page.identifier
        page_suggestions = by_page.get(page_key) or by_page.get(page.identifier) or []
        if not page_suggestions:
            continue

        current_html = page.html_content
        page_changed = False
        for s in page_suggestions:
            new_html = apply_suggestion_to_page(current_html, s)
            if new_html is None:
                not_applied.append(
                    {"suggestion_id": s.id, "reason": "target not found in page"}
                )
                continue
            current_html = new_html
            applied += 1
            page_changed = True

        if page_changed:
            sanitized, _ = validator.sanitize(current_html)
            patched = page.model_copy(update={"html_content": sanitized})
            patched_pages.append(patched)

    build_imscc(parsed, patched_pages, output_path)

    return {
        "input_path": str(input_path),
        "output_path": str(output_path),
        "applied_count": applied,
        "not_applied_count": len(not_applied),
        "not_applied": not_applied,
        "pages_modified": len(patched_pages),
    }


def register_builtin_handlers(dispatcher: Dispatcher) -> None:
    dispatcher.register("ping", ping)
    dispatcher.register("version", version)
    dispatcher.register("analyze_html", analyze_html)
    dispatcher.register("parse_imscc", parse_imscc)
    dispatcher.register("analyze_imscc", analyze_imscc)
    dispatcher.register("rendered_scan", rendered_scan)
    dispatcher.register("ai_status", ai_status)
    dispatcher.register("ai_generate_alt_text", ai_generate_alt_text)
    dispatcher.register("remediate_imscc", remediate_imscc)
    dispatcher.register("convert_document", convert_document)
    dispatcher.register("inventory_documents", inventory_documents)
    dispatcher.register("export_acr", export_acr)
    dispatcher.register("apply_suggestions", apply_suggestions)
