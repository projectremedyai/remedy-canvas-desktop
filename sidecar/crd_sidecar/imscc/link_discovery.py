"""Inventory PDF/DOCX/PPTX document links across every HTML page in an IMSCC.

Canvas exports reference uploaded files via the `$IMS-CC-FILEBASE$` placeholder
which resolves to the `web_resources/` tree inside the zip. A single PDF
linked from three pages should be converted once and the three source links
rewritten to the same new wiki page — so this module dedups by resolved
zip-member path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import unquote, urlparse

from bs4 import BeautifulSoup

from crd_sidecar.imscc.models import ParsedIMSCC

# Extensions the document-conversion pipeline can handle. Kept tight — other
# link types (mp4, zip, misc) should stay untouched in the IMSCC output.
DOCUMENT_EXTENSIONS: frozenset[str] = frozenset({"pdf", "docx", "doc", "pptx", "ppt"})

_FILEBASE_MARKER = "$IMS-CC-FILEBASE$"


@dataclass
class DocumentLink:
    """One unique document referenced from the course's HTML pages."""

    # The original href token as written in the HTML (may be URL-encoded,
    # may include Canvas `?canvas_download=1` query strings). Preserved so
    # the rewriter can find the exact string again.
    original_hrefs: list[str] = field(default_factory=list)

    # Zip member path — e.g. "web_resources/Uploaded Media/foo.pdf".
    # None for external http(s) links that happen to end in .pdf.
    resolved_path: str | None = None

    # True if the link is to an absolute http(s) URL, not a bundled file.
    is_external: bool = False

    # Canonical lowercased extension without the dot: "pdf", "docx", etc.
    extension: str = ""

    # Identifiers of the pages that reference this document.
    source_page_ids: list[str] = field(default_factory=list)

    # Best-effort human label from the first link's anchor text or title.
    display_text: str = ""


def discover_document_links(parsed: ParsedIMSCC) -> list[DocumentLink]:
    """Walk every HTML page in the archive; return a deduped list of
    PDF/DOCX/PPTX references.

    The result's ordering is deterministic: external links sort after
    bundled files, bundled files sort by resolved_path.
    """
    # Build a set of actual zip members so we can sanity-check resolved
    # paths without reopening the archive. ParsedIMSCC keeps the manifest
    # bytes but not a member list — for now we don't validate existence
    # against the zip; the converter service (Phase 5.5b) will do that
    # when it extracts the file.
    by_resolved: dict[str, DocumentLink] = {}
    externals: list[DocumentLink] = []

    for page in parsed.pages:
        soup = BeautifulSoup(page.html_content, "html.parser")
        for anchor in soup.find_all("a"):
            href = anchor.get("href")
            if not href:
                continue
            ext = _extract_extension(href)
            if ext not in DOCUMENT_EXTENSIONS:
                continue

            is_external = _is_external_url(href)
            resolved_path = None if is_external else _resolve_filebase(href)
            display_text = _best_display_text(anchor)

            if is_external:
                # Dedup externals by the plain URL (ignoring query) so the
                # same external PDF linked many times appears once.
                key = _normalize_external(href)
                link = next(
                    (e for e in externals if _normalize_external(e.original_hrefs[0]) == key),
                    None,
                )
                if link is None:
                    link = DocumentLink(
                        is_external=True,
                        extension=ext,
                        display_text=display_text,
                    )
                    externals.append(link)
            else:
                if resolved_path is None:
                    # Could not resolve — shouldn't happen for filebase
                    # hrefs but guard anyway. Skip; converter can't extract
                    # something we can't locate.
                    continue
                link = by_resolved.setdefault(
                    resolved_path,
                    DocumentLink(
                        resolved_path=resolved_path,
                        is_external=False,
                        extension=ext,
                        display_text=display_text,
                    ),
                )

            if href not in link.original_hrefs:
                link.original_hrefs.append(href)
            if page.identifier not in link.source_page_ids:
                link.source_page_ids.append(page.identifier)
            # Prefer the longest non-empty display text across all
            # occurrences — richer for naming the converted page.
            if display_text and len(display_text) > len(link.display_text):
                link.display_text = display_text

    return sorted(by_resolved.values(), key=lambda x: x.resolved_path or "") + externals


def _extract_extension(href: str) -> str:
    # Strip query/fragment; take the final .ext
    path = urlparse(href).path
    # Decode URL-encoded characters so ".pdf" still matches under %20 etc.
    decoded = unquote(path)
    _, _, tail = decoded.rpartition(".")
    return tail.lower().strip() if "." in decoded else ""


def _is_external_url(href: str) -> bool:
    scheme = urlparse(href).scheme.lower()
    return scheme in {"http", "https", "ftp", "mailto"}


def _resolve_filebase(href: str) -> str | None:
    """Translate `$IMS-CC-FILEBASE$/foo/bar.pdf` → `web_resources/foo/bar.pdf`.

    Returns None if the href doesn't use the filebase placeholder.
    """
    if _FILEBASE_MARKER not in href:
        return None
    # The href may be `$IMS-CC-FILEBASE$/Uploaded%20Media/foo.pdf?canvas_=1`.
    # Strip the placeholder + any query/fragment, URL-decode the rest,
    # then prefix with the canonical Canvas web_resources/ root.
    after = href.split(_FILEBASE_MARKER, 1)[1]
    # Drop leading slash so the placeholder/path stays clean.
    if after.startswith("/"):
        after = after[1:]
    path_part = urlparse(after).path
    decoded = unquote(path_part)
    return f"web_resources/{decoded}"


def _normalize_external(href: str) -> str:
    parsed = urlparse(href)
    return f"{parsed.scheme}://{parsed.netloc}{unquote(parsed.path)}"


def _best_display_text(anchor) -> str:
    title = (anchor.get("title") or "").strip()
    if title:
        return title
    text = (anchor.get_text() or "").strip()
    return text
