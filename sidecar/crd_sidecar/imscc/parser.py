"""Parse an IMSCC archive into a ParsedIMSCC (pages + manifest inventory).

Phase 2: wiki pages.
Phase 2.1: assignment bodies, discussion bodies, quiz question HTML, syllabus.
Phase 5.5: consume the resource inventory to drive PDF/DOCX/PPTX → new-page
            conversion with link rewriting.
"""

from __future__ import annotations

import html
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from defusedxml.ElementTree import fromstring as _safe_fromstring

from crd_sidecar.imscc.safe_archive import (
    read_member_bounded,
    validate_archive,
)

from crd_sidecar.crd_core.models import ContentType, CoursePage
from crd_sidecar.imscc.models import IMSCCResource, ParsedIMSCC

# IMS Common Cartridge v1.1 manifest namespace.
IMSCP_NS = "http://www.imsglobal.org/xsd/imsccv1p1/imscp_v1p1"
LOMIMSCC_NS = "http://ltsc.ieee.org/xsd/imsccv1p1/LOM/manifest"

# Resource types we recognize.
RES_TYPE_DISCUSSION = "imsdt_xmlv1p1"
RES_TYPE_QUIZ_PREFIX = "imsqti_xmlv1p2"
RES_TYPE_WEBCONTENT = "webcontent"
RES_TYPE_ASSOCIATED_PREFIX = "associatedcontent/"

# Inner content namespaces.
IMSDT_NS = "http://www.imsglobal.org/xsd/imsccv1p1/imsdt_v1p1"
QTI_NS = "http://www.imsglobal.org/xsd/ims_qtiasiv1p2"

# Canonical Canvas path for the syllabus body.
_SYLLABUS_PATH = "course_settings/syllabus.html"


def parse(archive_path: str | Path) -> ParsedIMSCC:
    """Open an IMSCC zip, read the manifest, collect every HTML-bearing page."""
    path = Path(archive_path)
    # Defend against zip bombs / malformed archives before any read.
    validate_archive(path)
    with zipfile.ZipFile(path) as zf:
        manifest_bytes = read_member_bounded(zf, "imsmanifest.xml")
        root = _safe_fromstring(manifest_bytes)

        course_title = _extract_course_title(root)
        schema_version = _extract_schema_version(root)
        resources = _collect_resources(root)
        pages = _collect_pages(zf, resources)

    return ParsedIMSCC(
        path=path,
        course_title=course_title,
        schema_version=schema_version,
        pages=pages,
        resources=resources,
        manifest_bytes=manifest_bytes,
    )


# ---------------------------------------------------------------------------
# Manifest helpers
# ---------------------------------------------------------------------------


def _extract_course_title(root: ET.Element) -> str:
    title_el = root.find(
        f".//{{{LOMIMSCC_NS}}}title/{{{LOMIMSCC_NS}}}string"
    )
    if title_el is not None and title_el.text:
        return title_el.text.strip()
    return "Untitled Course"


def _extract_schema_version(root: ET.Element) -> str:
    sv = root.find(f"{{{IMSCP_NS}}}metadata/{{{IMSCP_NS}}}schemaversion")
    if sv is not None and sv.text:
        return sv.text.strip()
    return "1.1.0"


def _collect_resources(root: ET.Element) -> list[IMSCCResource]:
    out: list[IMSCCResource] = []
    resources_el = root.find(f"{{{IMSCP_NS}}}resources")
    if resources_el is None:
        return out
    for r in resources_el.findall(f"{{{IMSCP_NS}}}resource"):
        identifier = r.get("identifier") or ""
        rtype = r.get("type") or ""
        href = r.get("href")
        files = [
            f.get("href", "")
            for f in r.findall(f"{{{IMSCP_NS}}}file")
            if f.get("href")
        ]
        out.append(IMSCCResource(identifier=identifier, type=rtype, href=href, files=files))
    return out


# ---------------------------------------------------------------------------
# Page collection — dispatches per resource type
# ---------------------------------------------------------------------------


def _collect_pages(
    zf: zipfile.ZipFile, resources: list[IMSCCResource]
) -> list[CoursePage]:
    pages: list[CoursePage] = []
    member_set = set(zf.namelist())
    seen_paths: set[str] = set()

    # Syllabus — Canvas puts it at a canonical path, not always in the manifest.
    if _SYLLABUS_PATH in member_set:
        syllabus_html = zf.read(_SYLLABUS_PATH).decode("utf-8", errors="replace")
        pages.append(
            CoursePage(
                id="syllabus",
                identifier="syllabus",
                title=_extract_page_title(syllabus_html, fallback="Syllabus"),
                content_type=ContentType.SYLLABUS,
                html_content=syllabus_html,
                file_path=_SYLLABUS_PATH,
            )
        )
        seen_paths.add(_SYLLABUS_PATH)

    for res in resources:
        if res.type == RES_TYPE_DISCUSSION:
            page = _parse_discussion(zf, res, member_set)
            if page is not None and page.file_path not in seen_paths:
                pages.append(page)
                seen_paths.add(page.file_path)
            continue

        if res.type.startswith(RES_TYPE_QUIZ_PREFIX):
            page = _parse_quiz(zf, res, member_set)
            if page is not None and page.file_path not in seen_paths:
                pages.append(page)
                seen_paths.add(page.file_path)
            continue

        # HTML files — wiki pages, assignment bodies, and any other .html
        # referenced either as webcontent or associatedcontent.
        if res.type == RES_TYPE_WEBCONTENT or res.type.startswith(
            RES_TYPE_ASSOCIATED_PREFIX
        ):
            for file_path in res.files:
                if not file_path.endswith(".html"):
                    continue
                if file_path in seen_paths or file_path not in member_set:
                    continue
                html_bytes = zf.read(file_path)
                html_str = html_bytes.decode("utf-8", errors="replace")
                content_type = _infer_html_content_type(res, file_path)
                title = _extract_page_title(html_str, fallback=file_path)
                pages.append(
                    CoursePage(
                        id=res.identifier,
                        identifier=res.identifier,
                        title=title,
                        content_type=content_type,
                        html_content=html_str,
                        file_path=file_path,
                    )
                )
                seen_paths.add(file_path)

    return pages


def _infer_html_content_type(res: IMSCCResource, file_path: str) -> ContentType:
    """Decide ContentType from the manifest resource + the file path."""
    if file_path == _SYLLABUS_PATH:
        return ContentType.SYLLABUS
    if file_path.startswith("wiki_content/"):
        return ContentType.WIKI_PAGE
    # Canvas assignments put the HTML body alongside `assignment_settings.xml`
    # in a `g<hash>/` directory, advertised via an associatedcontent resource.
    if any(f.endswith("assignment_settings.xml") for f in res.files):
        return ContentType.ASSIGNMENT
    return ContentType.WIKI_PAGE


# ---------------------------------------------------------------------------
# Discussion — XML wrapper with entity-encoded HTML in <text>
# ---------------------------------------------------------------------------


def _parse_discussion(
    zf: zipfile.ZipFile, res: IMSCCResource, member_set: set[str]
) -> CoursePage | None:
    xml_path = res.href or (res.files[0] if res.files else None)
    if not xml_path or xml_path not in member_set:
        return None

    root = _safe_fromstring(read_member_bounded(zf, xml_path))
    title_el = root.find(f"{{{IMSDT_NS}}}title")
    text_el = root.find(f"{{{IMSDT_NS}}}text")
    if text_el is None:
        return None

    # ET already decoded entities — text_el.text is real HTML.
    body = text_el.text or ""
    title = (
        title_el.text.strip()
        if title_el is not None and title_el.text
        else "Discussion"
    )
    return CoursePage(
        id=res.identifier,
        identifier=res.identifier,
        title=title,
        content_type=ContentType.DISCUSSION,
        html_content=body,
        file_path=xml_path,
    )


# ---------------------------------------------------------------------------
# Quiz — QTI XML with entity-encoded HTML in <mattext> elements
# ---------------------------------------------------------------------------


def _parse_quiz(
    zf: zipfile.ZipFile, res: IMSCCResource, member_set: set[str]
) -> CoursePage | None:
    # Prefer assessment_qti.xml (Canvas Canvas-Canvas format); fall back to
    # non_cc_assessments/*.xml.qti if needed.
    xml_path = next(
        (
            f
            for f in res.files
            if f.endswith("assessment_qti.xml") or f.endswith(".xml.qti")
        ),
        res.href,
    )
    if not xml_path or xml_path not in member_set:
        return None

    root = _safe_fromstring(read_member_bounded(zf, xml_path))
    assessment_el = root.find(f".//{{{QTI_NS}}}assessment")
    title = (
        assessment_el.get("title", "Quiz")
        if assessment_el is not None
        else "Quiz"
    )

    # Collect every mattext HTML chunk in document order.
    mattext_els = root.findall(f".//{{{QTI_NS}}}mattext")
    chunks = [m.text for m in mattext_els if m.text and m.text.strip()]

    # Concatenate for analysis. Phase 2.1 is parse-only for quizzes; the
    # builder refuses to patch them. Phase 5 will decide the remediation UX.
    concatenated = "\n".join(
        f'<div data-quiz-mattext-index="{i}">{c}</div>'
        for i, c in enumerate(chunks)
    )
    return CoursePage(
        id=res.identifier,
        identifier=res.identifier,
        title=title,
        content_type=ContentType.QUIZ,
        html_content=concatenated,
        file_path=xml_path,
    )


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------


def _extract_page_title(html_text: str, fallback: str) -> str:
    """Read the wiki page <title> element; fall back to the file path."""
    start = html_text.find("<title>")
    end = html_text.find("</title>", start + 7)
    if start != -1 and end != -1:
        title = html.unescape(html_text[start + 7 : end]).strip()
        return title or fallback
    return fallback
