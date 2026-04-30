"""IMSCC (IMS Common Cartridge) parser + builder.

Phase 2: wiki pages round-trip.
Phase 2.1: syllabus, assignments, discussions, quiz question HTML.
Phase 5.5: create new wiki-page resources for PDF/DOCX/PPTX conversions.
"""

from crd_sidecar.imscc.builder import build
from crd_sidecar.imscc.document_converter import (
    ConvertedDocument,
    DocumentConversionResult,
    convert_and_rewrite,
)
from crd_sidecar.imscc.image_fetcher import (
    IMSCCImageFetcher,
    resolve_filebase,
)
from crd_sidecar.imscc.link_discovery import (
    DOCUMENT_EXTENSIONS,
    DocumentLink,
    discover_document_links,
)
from crd_sidecar.imscc.models import IMSCCResource, ParsedIMSCC
from crd_sidecar.imscc.parser import parse

__all__ = [
    "parse",
    "build",
    "ParsedIMSCC",
    "IMSCCResource",
    "DocumentLink",
    "discover_document_links",
    "DOCUMENT_EXTENSIONS",
    "ConvertedDocument",
    "DocumentConversionResult",
    "convert_and_rewrite",
    "IMSCCImageFetcher",
    "resolve_filebase",
]

