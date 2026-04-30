"""Generate descriptive link-text suggestions for LNK004 residuals.

LNK004 fires for generic link text like "click here" / "read more" that
the deterministic transformer refuses to rewrite. Requires an LLM call
to look at the surrounding paragraph and the link's href target, then
propose concrete descriptive text.
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


async def generate_link_text_suggestion(
    issue: AccessibilityIssue,
    page_html: str,
    client: VisionClient,
    *,
    page_title: Optional[str] = None,
    page_file_path: Optional[str] = None,
) -> Optional[FixSuggestion]:
    """Produce a FixSuggestion for an LNK004 issue, or None on failure.

    The generator walks the page's HTML to locate the offending link
    (matched on href + text), pulls the enclosing paragraph as context,
    and asks the text LLM for replacement text + a confidence score.
    """
    if issue.rule_id != "LNK004":
        return None
    if not issue.element_html:
        return None

    anchor, context = _locate_anchor(page_html, issue.element_html)
    if anchor is None:
        return None

    original_text = anchor.get_text(strip=True)
    href = anchor.get("href") or ""
    if not original_text:
        return None

    prompt = _build_prompt(
        original_text=original_text,
        href=href,
        context=context,
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
            "link_text_suggestion_llm_failed",
            issue_id=issue.id,
            error=str(exc),
        )
        return None

    parsed = _parse_response(raw if isinstance(raw, str) else "")
    if parsed is None:
        return None

    proposed_text, confidence, rationale = parsed
    if not proposed_text or proposed_text.lower() == original_text.lower():
        return None

    proposed_anchor = _clone_with_new_text(anchor, proposed_text)
    return FixSuggestion(
        id=str(uuid.uuid4()),
        issue_id=issue.id,
        page_id=issue.page_id,
        page_title=page_title,
        page_file_path=page_file_path,
        rule_id=issue.rule_id,
        category=str(issue.category.value if hasattr(issue.category, "value") else issue.category),
        original_html=str(anchor),
        proposed_html=str(proposed_anchor),
        original_text=original_text,
        proposed_text=proposed_text,
        rationale=rationale,
        confidence=confidence,
    )


_SYSTEM_PROMPT = (
    "You rewrite non-descriptive link text (like 'click here' or 'read more') "
    "into concrete text that describes where the link goes. You return JSON only. "
    "Keep the text short (2-8 words), specific, and natural. Never include the "
    "phrase 'click' or 'here'. If you cannot improve the link given the context, "
    'return {"text": "", "confidence": 0.0, "rationale": "insufficient context"}.'
)


def _build_prompt(
    *,
    original_text: str,
    href: str,
    context: str,
    page_title: Optional[str],
) -> str:
    parts: list[str] = []
    if page_title:
        parts.append(f"Page title: {page_title}")
    parts.append(f"Surrounding paragraph: {context}")
    parts.append(f'Current link text: "{original_text}"')
    parts.append(f"Link destination URL: {href}")
    parts.append(
        'Return JSON: {"text": "<new link text>", '
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
    text = str(obj.get("text") or "").strip()
    try:
        confidence = float(obj.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    rationale = str(obj.get("rationale") or "").strip()
    confidence = max(0.0, min(1.0, confidence))
    return text, confidence, rationale


def _locate_anchor(page_html: str, element_html: str) -> tuple[Optional[Tag], str]:
    """Find the anchor matching element_html inside the full page.

    Returns (anchor_tag, enclosing_paragraph_text). Falls back to the
    page body if no enclosing block-level ancestor is found.
    """
    page_soup = BeautifulSoup(page_html, "html.parser")
    element_soup = BeautifulSoup(element_html, "html.parser")
    target = element_soup.find("a")
    if target is None:
        return None, ""

    target_href = (target.get("href") or "").strip()
    target_text = target.get_text(strip=True).lower()

    for link in page_soup.find_all("a"):
        href = (link.get("href") or "").strip()
        text = link.get_text(strip=True).lower()
        if href == target_href and text == target_text:
            return link, _paragraph_context(link)

    return None, ""


def _paragraph_context(anchor: Tag, max_chars: int = 400) -> str:
    """Pull the innerText of the nearest block-level ancestor, truncated."""
    block_ancestors = ("p", "li", "td", "th", "dd", "blockquote", "figcaption")
    parent: Optional[Tag] = anchor.parent
    while parent is not None:
        if getattr(parent, "name", None) in block_ancestors:
            break
        parent = parent.parent
    if parent is None:
        parent = anchor.parent or anchor

    text = parent.get_text(" ", strip=True)
    if len(text) > max_chars:
        text = text[: max_chars - 1].rstrip() + "…"
    return text


def _clone_with_new_text(anchor: Tag, new_text: str) -> Tag:
    """Return a copy of the anchor with inner text replaced."""
    cloned_soup = BeautifulSoup(str(anchor), "html.parser")
    cloned = cloned_soup.find("a")
    if cloned is None:
        return anchor
    cloned.clear()
    cloned.append(new_text)
    return cloned


def apply_link_text_suggestion(
    page_html: str,
    *,
    original_text: str,
    proposed_text: str,
    href: str,
) -> Optional[str]:
    """Rewrite the first anchor matching (href, original_text) to use
    proposed_text. Returns the updated page HTML, or None when no
    matching anchor is found.

    Matching deliberately ignores attribute ordering and serialization
    quirks — we re-parse the page fresh and look for content-equivalent
    anchors. This mirrors the generator's `_locate_anchor` strategy so
    suggestions that were generatable are also applyable.
    """
    if not original_text or not proposed_text:
        return None

    soup = BeautifulSoup(page_html, "html.parser")
    norm_href = (href or "").strip()
    norm_text = original_text.strip().lower()

    for link in soup.find_all("a"):
        link_href = (link.get("href") or "").strip()
        link_text = link.get_text(strip=True).lower()
        if link_href == norm_href and link_text == norm_text:
            link.clear()
            link.append(proposed_text)
            return str(soup)

    return None
