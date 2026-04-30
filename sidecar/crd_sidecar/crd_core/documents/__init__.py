"""PDF/DOCX/PPTX → accessible HTML via LiteParse + an LLM."""

from __future__ import annotations

from pathlib import Path

from crd_sidecar.crd_core.documents.liteparse_adapter import (
    LiteParseAdapter,
    SpatialParseResult,
)
from crd_sidecar.crd_core.documents.llm_converter import (
    DocumentToHTMLService,
)


async def convert_document_to_html(
    file_path: str | Path,
    *,
    title: str = "",
) -> str:
    """One-shot: parse a document file with LiteParse, convert to accessible HTML.

    Uses the LLM defaults from crd_core.config (qwen3.5:4b by default).
    Blocks until the whole document is processed. A 300+-page document
    can take minutes — the Tauri shell should show a spinner or phase
    event while this runs; streaming progress through the pipeline is
    a future add (the converter has a cancel_check hook we aren't
    wiring yet).
    """
    path = Path(file_path).expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"document not found: {path}")

    adapter = LiteParseAdapter()
    spatial: SpatialParseResult = adapter.parse_file(str(path))

    service = DocumentToHTMLService()
    html = await service.convert(spatial, title=title or path.stem)
    return html


__all__ = [
    "convert_document_to_html",
    "LiteParseAdapter",
    "SpatialParseResult",
    "DocumentToHTMLService",
]
