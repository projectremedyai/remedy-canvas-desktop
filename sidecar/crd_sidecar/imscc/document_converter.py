"""Convert every bundled document in an IMSCC to a new wiki page +
rewrite the source HTML links to point at those new pages.

Flow per course:
    1. Discover every <a href> to a bundled PDF/DOCX/PPTX via link_discovery.
    2. For each unique document: extract from the zip → LiteParse + LLM →
       accessible HTML fragment.
    3. Assign a stable identifier + wiki_content path to each converted doc.
    4. Build a rewrite map: every original href → `$WIKI_REFERENCE$/pages/<slug>`.
    5. Apply the rewrites across the existing HTML pages (in memory).
    6. Serialize the new resources into the IMSCC manifest XML.
    7. Hand the builder the modified pages + new files + new manifest.

The resulting IMSCC, when re-imported into Canvas, produces one new wiki
page per document with the accessible HTML. Canvas's page linker honors
`$WIKI_REFERENCE$/pages/<slug>` on import.

Externals (http/https PDF links) pass through unchanged — we have no zip
bytes for them, and downloading from an outside site isn't part of the
desktop app's offline promise.
"""

from __future__ import annotations

import hashlib
import html
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable
from xml.etree import ElementTree as ET

from defusedxml.ElementTree import fromstring as _safe_fromstring

from crd_sidecar.crd_core.models import CoursePage
from crd_sidecar.imscc.link_discovery import (
    DocumentLink,
    discover_document_links,
)
from crd_sidecar.imscc.models import ParsedIMSCC
from crd_sidecar.imscc.parser import IMSCP_NS

ProgressCallback = Callable[[dict[str, Any]], None]

# Default converter: the real LLM-backed pipeline. Tests can inject a stub
# to avoid spinning up Ollama + LiteParse for every run.
AsyncDocConverter = Callable[[str, str], Awaitable[str]]


@dataclass
class ConvertedDocument:
    """One document that was extracted → converted → injected as a wiki page."""

    link: DocumentLink
    identifier: str                 # new manifest resource identifier
    slug: str                       # URL-safe path component
    new_file_path: str              # e.g. "wiki_content/syllabus.html"
    wiki_reference: str             # e.g. "$WIKI_REFERENCE$/pages/syllabus"
    html_bytes: bytes               # the full wiki-page HTML document


@dataclass
class DocumentConversionResult:
    """Everything needed to produce the remediated IMSCC."""

    converted: list[ConvertedDocument] = field(default_factory=list)
    rewritten_pages: list[CoursePage] = field(default_factory=list)
    new_files: dict[str, bytes] = field(default_factory=dict)
    manifest_bytes: bytes | None = None
    skipped: list[DocumentLink] = field(default_factory=list)


async def convert_and_rewrite(
    parsed: ParsedIMSCC,
    *,
    converter: AsyncDocConverter,
    on_progress: ProgressCallback | None = None,
) -> DocumentConversionResult:
    """Run the full Phase 5.5b flow on a ParsedIMSCC.

    `converter` turns (extracted_file_path, title) into accessible HTML.
    Separated out so tests don't need a live LLM. Production callers pass
    `crd_core.documents.convert_document_to_html`.
    """

    def emit(phase: str, **payload: Any) -> None:
        if on_progress is not None:
            on_progress({"phase": phase, **payload})

    links = discover_document_links(parsed)
    bundled = [link for link in links if not link.is_external and link.resolved_path]
    emit(
        "docs.discovered",
        total=len(links),
        bundled=len(bundled),
        external=len(links) - len(bundled),
    )

    result = DocumentConversionResult()
    if not bundled:
        return result

    # Extract every bundled document to a scratch dir so the converter
    # can read real files. The dir stays around until the caller exits.
    import tempfile

    tmp_root = Path(tempfile.mkdtemp(prefix="remedy-canvas-desktop-docs-"))
    try:
        existing_paths = _list_archive_members(parsed.path)
        used_filenames: set[str] = {
            p for p in existing_paths if p.startswith("wiki_content/")
        }

        for idx, link in enumerate(bundled, start=1):
            emit(
                "doc.start",
                index=idx,
                total=len(bundled),
                resolved_path=link.resolved_path,
                display_text=link.display_text,
                extension=link.extension,
            )

            if link.resolved_path not in existing_paths:
                result.skipped.append(link)
                emit(
                    "doc.skip",
                    index=idx,
                    resolved_path=link.resolved_path,
                    reason="not present in archive",
                )
                continue

            local_path = _extract_member(parsed.path, link.resolved_path, tmp_root)
            title = link.display_text or Path(link.resolved_path).stem

            try:
                html_fragment = await converter(str(local_path), title)
            except Exception as exc:  # noqa: BLE001 — keep going on bad docs
                result.skipped.append(link)
                emit(
                    "doc.skip",
                    index=idx,
                    resolved_path=link.resolved_path,
                    reason=f"{type(exc).__name__}: {exc}",
                )
                continue

            slug = _unique_slug(title, taken_filenames=used_filenames)
            new_file_path = f"wiki_content/{slug}.html"
            used_filenames.add(new_file_path)

            identifier = _stable_identifier(link.resolved_path)
            page_bytes = _wrap_as_wiki_page_html(title, html_fragment, identifier)

            converted = ConvertedDocument(
                link=link,
                identifier=identifier,
                slug=slug,
                new_file_path=new_file_path,
                wiki_reference=f"$WIKI_REFERENCE$/pages/{slug}",
                html_bytes=page_bytes,
            )
            result.converted.append(converted)
            result.new_files[new_file_path] = page_bytes
            emit(
                "doc.done",
                index=idx,
                resolved_path=link.resolved_path,
                new_file_path=new_file_path,
                identifier=identifier,
                slug=slug,
                html_size=len(page_bytes),
            )
    finally:
        # Scratch files were only needed during extraction/conversion.
        import shutil

        shutil.rmtree(tmp_root, ignore_errors=True)

    if result.converted:
        result.rewritten_pages = _rewrite_links_across_pages(parsed.pages, result.converted)
        result.manifest_bytes = _register_resources_in_manifest(
            parsed.manifest_bytes, result.converted
        )

    emit(
        "docs.completed",
        converted=len(result.converted),
        skipped=len(result.skipped),
    )
    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _list_archive_members(archive_path: Path) -> set[str]:
    with zipfile.ZipFile(archive_path) as zf:
        return set(zf.namelist())


def _extract_member(archive_path: Path, member: str, dest_root: Path) -> Path:
    local_path = dest_root / member
    local_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive_path) as zf:
        with zf.open(member) as src, local_path.open("wb") as dst:
            dst.write(src.read())
    return local_path


def _slugify(value: str) -> str:
    """Lowercase, alnum-only-with-dashes, deduped dashes, trimmed."""
    lowered = value.lower().strip()
    lowered = re.sub(r"[^a-z0-9]+", "-", lowered)
    lowered = re.sub(r"-{2,}", "-", lowered).strip("-")
    return lowered or "converted-document"


def _unique_slug(title: str, *, taken_filenames: set[str]) -> str:
    base = _slugify(title)
    candidate = base
    counter = 2
    while f"wiki_content/{candidate}.html" in taken_filenames:
        candidate = f"{base}-{counter}"
        counter += 1
    return candidate


def _stable_identifier(resolved_path: str) -> str:
    """SHA1-based identifier that's stable across runs for the same doc."""
    digest = hashlib.sha1(resolved_path.encode("utf-8")).hexdigest()[:24]
    # IMSCC identifiers conventionally start with a lowercase letter.
    return f"clu_doc_{digest}"


def _wrap_as_wiki_page_html(title: str, body_html: str, identifier: str) -> bytes:
    """Match the Canvas wiki-page HTML shape so the re-import produces a
    real wiki page with the right title and identifier meta tags.
    """
    escaped_title = html.escape(title)
    doc = (
        "<html>\n"
        "<head>\n"
        '<meta http-equiv="Content-Type" content="text/html; charset=utf-8"/>\n'
        f"<title>{escaped_title}</title>\n"
        f'<meta name="identifier" content="{identifier}"/>\n'
        '<meta name="editing_roles" content="teachers"/>\n'
        '<meta name="workflow_state" content="active"/>\n'
        "</head>\n"
        "<body>\n"
        f"{body_html}\n"
        "</body>\n"
        "</html>\n"
    )
    return doc.encode("utf-8")


def _href_variants(decoded: str) -> list[str]:
    """Yield the plausible literal forms of an href string as it might appear
    in the raw HTML. BeautifulSoup decodes HTML entities when you read an
    attribute, so `anchor.get("href")` returns `foo?a=1&b=2` even when the
    raw source is `foo?a=1&amp;b=2`. Any literal-string rewrite has to try
    both forms or it silently no-ops on every ampersand-bearing querystring
    (which is nearly every Canvas file link).
    """
    variants: list[str] = [decoded]
    # The most common entity in real Canvas hrefs is &amp;; &#38; and
    # &#x26; also exist but are vanishingly rare in exports. Add them
    # defensively so we don't have to revisit this for one-off cases.
    if "&" in decoded:
        for entity in ("&amp;", "&#38;", "&#x26;"):
            v = decoded.replace("&", entity)
            if v not in variants:
                variants.append(v)
    return variants


def _rewrite_links_across_pages(
    pages: list[CoursePage], converted: list[ConvertedDocument]
) -> list[CoursePage]:
    """Return new CoursePage objects with hrefs redirected to wiki references.

    Only yields pages whose HTML actually changed — the orchestrator
    feeds these into the builder as remediated overrides.
    """
    # Map EVERY original href token → the wiki reference for its converted page.
    # Key is the BeautifulSoup-decoded href recorded by link_discovery; we
    # expand to raw variants at rewrite time (see _href_variants).
    href_to_wiki: dict[str, str] = {}
    for doc in converted:
        for original_href in doc.link.original_hrefs:
            href_to_wiki[original_href] = doc.wiki_reference

    if not href_to_wiki:
        return []

    rewritten: list[CoursePage] = []
    for page in pages:
        html_content = page.html_content
        modified = False
        for original, wiki_ref in href_to_wiki.items():
            for variant in _href_variants(original):
                if variant not in html_content:
                    continue
                html_content = html_content.replace(
                    f'href="{variant}"', f'href="{wiki_ref}"'
                )
                html_content = html_content.replace(
                    f"href='{variant}'", f"href='{wiki_ref}'"
                )
                modified = True
        if modified and html_content != page.html_content:
            rewritten.append(page.model_copy(update={"html_content": html_content}))
    return rewritten


def _register_resources_in_manifest(
    manifest_bytes: bytes, converted: list[ConvertedDocument]
) -> bytes:
    """Add one <resource type="webcontent"> entry per converted document.

    Preserves the default namespace on output (ET namespace-registers
    before serializing). Adds entries directly inside <resources>, at the
    end — matches Canvas export convention.
    """
    # Register the namespace prefix so serialization doesn't emit ns0:.
    ET.register_namespace("", IMSCP_NS)
    ET.register_namespace(
        "lomimscc", "http://ltsc.ieee.org/xsd/imsccv1p1/LOM/manifest"
    )

    root = _safe_fromstring(manifest_bytes)
    resources_el = root.find(f"{{{IMSCP_NS}}}resources")
    if resources_el is None:
        raise ValueError("manifest missing <resources> element")

    for doc in converted:
        resource_el = ET.SubElement(
            resources_el,
            f"{{{IMSCP_NS}}}resource",
            {
                "identifier": doc.identifier,
                "type": "webcontent",
                "href": doc.new_file_path,
            },
        )
        ET.SubElement(
            resource_el,
            f"{{{IMSCP_NS}}}file",
            {"href": doc.new_file_path},
        )

    return ET.tostring(root, encoding="utf-8", xml_declaration=True)
