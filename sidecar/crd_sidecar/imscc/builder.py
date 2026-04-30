"""Write a remediated IMSCC archive.

Strategy: copy every entry from the source archive verbatim, except for the
files that the caller has supplied a remediated CoursePage for — those get
overwritten. Serialization depends on the content type:

    WIKI_PAGE / ASSIGNMENT / SYLLABUS:  direct HTML overwrite
    DISCUSSION:                         string-level replace inside the XML's
                                        <text texttype="text/html"> element,
                                        entity-encoding the new HTML so the
                                        rest of the file stays byte-identical
    QUIZ:                               not supported in Phase 2.1 — a quiz
                                        CoursePage packs many <mattext>
                                        chunks into one html_content blob
                                        and there's no reliable way to split
                                        them back out. Rejected loudly.

Phase 5.5 will also add NEW manifest entries (for converted PDFs) and new
wiki-page files — a different code path handled there.
"""

from __future__ import annotations

import html as html_lib
import re
import zipfile
from pathlib import Path

from crd_sidecar.crd_core.models import ContentType, CoursePage
from crd_sidecar.imscc.models import ParsedIMSCC

_DISCUSSION_TEXT_PATTERN = re.compile(
    r"(<text[^>]*>)(.*?)(</text>)", re.DOTALL
)


def build(
    parsed: ParsedIMSCC,
    remediated_pages: list[CoursePage],
    output_path: str | Path,
    *,
    new_files: dict[str, bytes] | None = None,
    manifest_override: bytes | None = None,
) -> Path:
    """Write a new IMSCC archive with remediated pages patched in.

    Phase 5.5b extensions:
        new_files: additional entries to write into the output zip, keyed
            by in-archive path. Used by the document converter to inject
            new wiki_content/*.html files for converted PDFs. Paths must
            not collide with anything already in the source archive.
        manifest_override: bytes to replace `imsmanifest.xml` with. Used
            to register new wiki-page resources for converted documents.
    """
    output = Path(output_path)
    new_files = new_files or {}

    # Validate every incoming page first so we fail before opening the zip.
    overrides = _prepare_overrides(parsed, remediated_pages)

    with zipfile.ZipFile(parsed.path) as zin, zipfile.ZipFile(
        output, "w", zipfile.ZIP_DEFLATED
    ) as zout:
        source_names = set(zin.namelist())

        # Collision check for new files.
        collisions = source_names & set(new_files)
        if collisions:
            raise ValueError(
                f"new_files collide with existing archive entries: {sorted(collisions)}"
            )

        written: set[str] = set()
        for info in zin.infolist():
            if info.filename == "imsmanifest.xml" and manifest_override is not None:
                zout.writestr(info, manifest_override)
            else:
                original_bytes = zin.read(info.filename)
                if info.filename in overrides:
                    new_bytes = overrides[info.filename](original_bytes)
                    zout.writestr(info, new_bytes)
                else:
                    zout.writestr(info, original_bytes)
            written.add(info.filename)

        # Append new files (converted document pages, etc.).
        for path, content in new_files.items():
            zout.writestr(path, content)
            written.add(path)

        missing = set(overrides) - written
        if missing:
            raise ValueError(
                f"remediated_pages reference file_paths absent from source archive: {sorted(missing)}"
            )

    return output


def _prepare_overrides(
    parsed: ParsedIMSCC, remediated_pages: list[CoursePage]
) -> dict[str, "OverrideFn"]:
    """Validate each remediated page and return a {file_path: override_fn} map.

    Each override_fn takes the original bytes of that entry and returns the
    bytes to write. Deferring evaluation lets the DISCUSSION path splice
    into the original XML instead of regenerating it.
    """
    overrides: dict[str, OverrideFn] = {}
    for page in remediated_pages:
        if not page.file_path:
            raise ValueError(
                f"CoursePage {page.identifier!r} has no file_path — cannot map "
                "it back into the IMSCC archive"
            )
        if page.file_path in overrides:
            raise ValueError(
                f"Duplicate remediation for file_path {page.file_path!r}"
            )

        if page.content_type in (
            ContentType.WIKI_PAGE,
            ContentType.ASSIGNMENT,
            ContentType.SYLLABUS,
            ContentType.ANNOUNCEMENT,
        ):
            new_bytes = page.html_content.encode("utf-8")
            overrides[page.file_path] = _const_bytes(new_bytes)

        elif page.content_type == ContentType.DISCUSSION:
            overrides[page.file_path] = _discussion_override(page.html_content)

        elif page.content_type in (
            ContentType.QUIZ,
            ContentType.QUIZ_QUESTION,
            ContentType.NEW_QUIZ,
            ContentType.NEW_QUIZ_ITEM,
        ):
            raise NotImplementedError(
                f"Patching QUIZ content is out of Phase 2.1 scope "
                f"(file_path={page.file_path!r}). A future phase will split "
                "the mattext chunks back out."
            )

        else:
            raise ValueError(
                f"Unsupported content_type for remediation: {page.content_type}"
            )

    return overrides


# --- per-content-type serializers ------------------------------------------


OverrideFn = "callable"  # lazy type alias for readability below


def _const_bytes(new_bytes: bytes):
    def _apply(_original: bytes) -> bytes:
        return new_bytes

    return _apply


def _discussion_override(remediated_html: str):
    """Return a function that splices remediated HTML into the <text> element,
    preserving every other byte of the discussion XML.
    """

    def _apply(original: bytes) -> bytes:
        source = original.decode("utf-8")
        match = _DISCUSSION_TEXT_PATTERN.search(source)
        if match is None:
            raise ValueError(
                "Discussion XML is missing a <text> element — cannot patch"
            )
        encoded = html_lib.escape(remediated_html, quote=False)
        start, end = match.start(2), match.end(2)
        patched = source[:start] + encoded + source[end:]
        return patched.encode("utf-8")

    return _apply
