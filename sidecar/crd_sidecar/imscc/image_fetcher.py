"""Resolve and extract images referenced from wiki HTML inside an IMSCC zip.

Canvas exports reference uploaded images via the `$IMS-CC-FILEBASE$` placeholder
that resolves to the `web_resources/` tree inside the archive. The Phase 5.1
AI alt-text pipeline needs to (a) figure out which zip member a given `<img src>`
maps to and (b) copy the bytes out to a temp path the vision client can read.

External `http(s)://` and `mailto:`/`ftp:` URLs are intentionally not fetched —
the desktop app is offline-first, and network fetches would be both slow and
non-deterministic. Callers get `None` for those and should move on.
"""

from __future__ import annotations

import shutil
import zipfile
from pathlib import Path
from urllib.parse import unquote, urlparse

from crd_sidecar.imscc.models import ParsedIMSCC

_FILEBASE_MARKER = "$IMS-CC-FILEBASE$"
_EXTERNAL_SCHEMES = frozenset({"http", "https", "ftp", "mailto", "data"})


def _is_external_url(href: str) -> bool:
    scheme = urlparse(href).scheme.lower()
    return scheme in _EXTERNAL_SCHEMES


def resolve_filebase(href: str) -> str | None:
    """Translate `$IMS-CC-FILEBASE$/foo/bar.jpg` → `web_resources/foo/bar.jpg`.

    Returns None if the href doesn't use the filebase placeholder. Mirrors
    the logic in `link_discovery._resolve_filebase` — kept as a shared
    utility so the alt-text pipeline and the document-link inventory stay
    in sync.
    """
    if _FILEBASE_MARKER not in href:
        return None
    after = href.split(_FILEBASE_MARKER, 1)[1]
    if after.startswith("/"):
        after = after[1:]
    path_part = urlparse(after).path
    decoded = unquote(path_part)
    return f"web_resources/{decoded}"


class IMSCCImageFetcher:
    """Resolve `<img src>` hrefs to zip members and extract bytes on demand.

    Bind a fetcher to a ParsedIMSCC (which carries the archive path); call
    `resolve_src_to_zip_path` to dry-run a lookup, `extract_to` to actually
    copy the bytes to a directory you control.

    The fetcher does NOT manage the temp directory for you — callers decide
    the lifecycle. Use `tempfile.mkdtemp()` + `shutil.rmtree()` or a
    `tempfile.TemporaryDirectory()` context manager.
    """

    def __init__(self, parsed: ParsedIMSCC) -> None:
        self._parsed = parsed
        self._archive_path: Path = Path(parsed.path)
        # Lazy-loaded set of zip member names so we can answer
        # "does this file actually exist in the archive?" without reopening
        # the zip for every call. Populated on first lookup.
        self._member_set: set[str] | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def resolve_src_to_zip_path(self, src: str) -> str | None:
        """Return the zip-member path for a given `<img src>`, or None.

        None is returned for:
          - external URLs (http, https, ftp, mailto, data)
          - hrefs that use `$IMS-CC-FILEBASE$` but don't resolve to an
            actual zip member
          - empty or whitespace-only hrefs
          - non-filebase relative paths that don't exist in the zip
        """
        if not src or not src.strip():
            return None
        src = src.strip()
        if _is_external_url(src):
            return None

        resolved = resolve_filebase(src)
        if resolved is None:
            # Might be a plain relative path like "web_resources/foo.jpg"
            # already. Accept it if it's actually present in the archive.
            candidate = unquote(urlparse(src).path).lstrip("/")
            if candidate and candidate in self._members():
                return candidate
            return None

        if resolved not in self._members():
            return None
        return resolved

    def extract_to(self, src: str, dest_dir: str | Path) -> Path | None:
        """Copy the image bytes referenced by `src` into `dest_dir`.

        Returns the path of the extracted file, or None if `src` couldn't
        be resolved to a zip member. The destination file keeps the
        original filename (last path component). Callers are responsible
        for cleaning up `dest_dir`.
        """
        zip_path = self.resolve_src_to_zip_path(src)
        if zip_path is None:
            return None

        dest_root = Path(dest_dir)
        dest_root.mkdir(parents=True, exist_ok=True)
        filename = Path(zip_path).name
        dest_path = dest_root / filename

        with zipfile.ZipFile(self._archive_path) as zf, zf.open(zip_path) as src_stream:
            with dest_path.open("wb") as out:
                shutil.copyfileobj(src_stream, out)
        return dest_path

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _members(self) -> set[str]:
        if self._member_set is None:
            with zipfile.ZipFile(self._archive_path) as zf:
                self._member_set = set(zf.namelist())
        return self._member_set


__all__ = ["IMSCCImageFetcher", "resolve_filebase"]
