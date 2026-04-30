"""Generate caption suggestions for TBL003 residuals.

TBL003 fires when a data table has no <caption>. The transformer
tries to synthesize one from context (a colspan-3 first cell, a bold
<p> immediately preceding the table, etc.) but deliberately leaves
the table uncaptioned if it can't find concrete context — a bogus
"Data table" caption is worse than none. Those uncaptioned tables
are the residuals the LLM can help with: given the table's headers
and a few rows, propose a concrete descriptive caption.

Tables don't have stable attributes to key on, so the applier
matches on the first-row text signature (unique within a page in
practice) plus a "no caption yet" filter.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Optional

import structlog
from bs4 import BeautifulSoup, Tag

from crd_sidecar.crd_core.ai.vision_client import VisionClient
from crd_sidecar.crd_core.models import AccessibilityIssue
from crd_sidecar.crd_core.suggestions.models import FixSuggestion

_logger = structlog.get_logger(__name__)


async def generate_table_caption_suggestion(
    issue: AccessibilityIssue,
    page_html: str,
    client: VisionClient,
    *,
    page_title: Optional[str] = None,
    page_file_path: Optional[str] = None,
) -> Optional[FixSuggestion]:
    """Produce a FixSuggestion for a TBL003 issue, or None on failure."""
    if issue.rule_id != "TBL003":
        return None
    if not issue.element_html:
        return None

    target_sig = _row_signature_from_fragment(issue.element_html)
    if not target_sig:
        return None

    table = _find_uncaptioned_table(page_html, target_sig)
    if table is None:
        return None

    preview = _table_preview(table)
    prompt = _build_prompt(
        preview=preview,
        page_title=page_title,
    )

    try:
        raw = await client.chat(
            model=client.get_fallback_model(),
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            timeout=30.0,
        )
    except Exception as exc:  # noqa: BLE001
        _logger.warning(
            "table_caption_suggestion_llm_failed",
            issue_id=issue.id,
            error=str(exc),
        )
        return None

    parsed = _parse_response(raw if isinstance(raw, str) else "")
    if parsed is None:
        return None
    proposed_text, confidence, rationale = parsed
    if not proposed_text:
        return None

    proposed_table = _clone_with_caption(table, proposed_text)
    return FixSuggestion(
        id=str(uuid.uuid4()),
        issue_id=issue.id,
        page_id=issue.page_id,
        page_title=page_title,
        page_file_path=page_file_path,
        rule_id=issue.rule_id,
        category=str(
            issue.category.value if hasattr(issue.category, "value") else issue.category
        ),
        original_html=_truncate_html(str(table), 400),
        proposed_html=_truncate_html(str(proposed_table), 500),
        original_text="",
        proposed_text=proposed_text,
        rationale=rationale,
        confidence=confidence,
    )


def apply_table_caption_suggestion(
    page_html: str,
    *,
    row_signature: str,
    proposed_caption: str,
) -> Optional[str]:
    """Prepend a <caption> to the first uncaptioned table whose first-row
    text signature matches ``row_signature``. Returns updated HTML or
    None on no match.
    """
    if not proposed_caption or not row_signature:
        return None
    soup = BeautifulSoup(page_html, "html.parser")
    for table in soup.find_all("table"):
        if table.find("caption"):
            continue
        sig = _row_signature(table)
        if sig and sig == row_signature:
            caption = soup.new_tag("caption")
            caption.string = proposed_caption
            caption["style"] = (
                "text-align: center; font-weight: bold; padding: 8px;"
            )
            table.insert(0, caption)
            return str(soup)
    return None


def row_signature_for_original(original_html: str) -> str:
    """Expose signature extraction so the dispatcher can compute it
    from the suggestion's stored original_html at apply time."""
    return _row_signature_from_fragment(original_html)


_SYSTEM_PROMPT = (
    "You write concise captions for data tables. Captions should state "
    "what the table contains — the relationship between rows and "
    "columns — in 3-10 words. Return JSON only. Avoid filler like "
    "'A table showing' or 'This table lists'. Prefer concrete nouns."
    'If the context is insufficient return {"text": "", "confidence": 0.0, '
    '"rationale": "unclear"}.'
)


def _build_prompt(*, preview: str, page_title: Optional[str]) -> str:
    parts: list[str] = []
    if page_title:
        parts.append(f"Page title: {page_title}")
    parts.append("Table preview (headers + first rows):")
    parts.append(preview)
    parts.append(
        'Return JSON: {"text": "<caption>", '
        '"confidence": <0.0 to 1.0>, '
        '"rationale": "<one short sentence>"}'
    )
    return "\n".join(parts)


_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse_response(raw: str) -> Optional[tuple[str, float, str]]:
    if not raw:
        return None
    match = _JSON_RE.search(raw)
    if not match:
        return None
    try:
        obj = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    text = str(obj.get("text") or "").strip().strip('"')
    try:
        confidence = float(obj.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    rationale = str(obj.get("rationale") or "").strip()
    confidence = max(0.0, min(1.0, confidence))
    return text, confidence, rationale


def _row_signature(table: Tag) -> str:
    """Join the text of the first row's cells with a separator. Used to
    locate a specific table uniquely within a page."""
    first_row = table.find("tr")
    if first_row is None:
        return ""
    cells: list[str] = []
    for cell in first_row.find_all(["th", "td"]):
        cells.append(cell.get_text(" ", strip=True))
    return " | ".join(cells)


def _row_signature_from_fragment(fragment: str) -> str:
    """Extract the row signature from a (possibly truncated) table
    fragment. Falls back to empty when the fragment doesn't contain
    a complete first row."""
    soup = BeautifulSoup(fragment, "html.parser")
    table = soup.find("table")
    if table is None:
        return ""
    return _row_signature(table)


def _find_uncaptioned_table(page_html: str, target_sig: str) -> Optional[Tag]:
    soup = BeautifulSoup(page_html, "html.parser")
    for table in soup.find_all("table"):
        if table.find("caption"):
            continue
        if _row_signature(table) == target_sig:
            return table
    return None


def _table_preview(table: Tag, max_rows: int = 4, max_cell_chars: int = 40) -> str:
    """Render a compact text preview of the table — one line per row,
    cells joined by '|', truncated per cell. Skips the whole preview
    if the table has no rows."""
    rows = table.find_all("tr")[:max_rows]
    lines: list[str] = []
    for row in rows:
        cells: list[str] = []
        for cell in row.find_all(["th", "td"]):
            text = cell.get_text(" ", strip=True)
            if len(text) > max_cell_chars:
                text = text[: max_cell_chars - 1].rstrip() + "…"
            cells.append(text)
        lines.append(" | ".join(cells))
    extra = len(table.find_all("tr")) - max_rows
    if extra > 0:
        lines.append(f"… ({extra} more row{'s' if extra != 1 else ''})")
    return "\n".join(lines)


def _clone_with_caption(table: Tag, caption_text: str) -> Tag:
    soup = BeautifulSoup(str(table), "html.parser")
    clone = soup.find("table")
    if clone is None:
        return table
    caption = soup.new_tag("caption")
    caption.string = caption_text
    caption["style"] = "text-align: center; font-weight: bold; padding: 8px;"
    clone.insert(0, caption)
    return clone


def _truncate_html(html: str, max_chars: int) -> str:
    if len(html) <= max_chars:
        return html
    return html[: max_chars - 1].rstrip() + "…"
