"""Generate label-text suggestions for FORM003 residuals.

FORM003 fires when a <label> element is present but empty. The
transformer can't safely guess what text belongs there — it depends
on the input the label is for and the surrounding context. LLM can
take the associated input + paragraph and propose a short, concrete
label.

Applier matches on the label's ``for`` attribute when present (stable
cross-rewrite), otherwise on the serialized attribute set.
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


async def generate_form_label_suggestion(
    issue: AccessibilityIssue,
    page_html: str,
    client: VisionClient,
    *,
    page_title: Optional[str] = None,
    page_file_path: Optional[str] = None,
) -> Optional[FixSuggestion]:
    """Produce a FixSuggestion for a FORM003 issue, or None on failure."""
    if issue.rule_id != "FORM003":
        return None
    if not issue.element_html:
        return None

    label, context_html, input_html = _locate_label(page_html, issue.element_html)
    if label is None:
        return None

    for_attr = (label.get("for") or "").strip()
    prompt = _build_prompt(
        context=context_html,
        input_html=input_html,
        for_attr=for_attr,
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
            "form_label_suggestion_llm_failed",
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

    proposed_label = _clone_with_text(label, proposed_text)
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
        original_html=str(label),
        proposed_html=str(proposed_label),
        original_text="",
        proposed_text=proposed_text,
        rationale=rationale,
        confidence=confidence,
    )


def apply_form_label_suggestion(
    page_html: str,
    *,
    for_attr: str,
    proposed_text: str,
) -> Optional[str]:
    """Set visible text on the first empty <label> matching ``for_attr``.

    When ``for_attr`` is empty, falls back to the first empty label on
    the page — useful for labels that wrap their input without using a
    for/id pair. Returns updated page HTML, or None on no match.
    """
    if not proposed_text:
        return None
    soup = BeautifulSoup(page_html, "html.parser")
    target = None
    for label in soup.find_all("label"):
        if _label_text(label):
            continue
        if for_attr:
            if (label.get("for") or "").strip() == for_attr:
                target = label
                break
        else:
            target = label
            break
    if target is None:
        return None
    target.append(proposed_text)
    return str(soup)


_SYSTEM_PROMPT = (
    "You fill in empty form labels with short, concrete text that names "
    "the associated input field. You return JSON only. Keep the text "
    "short (1-6 words), specific, and natural — like 'Email address' or "
    "'Course name'. Never use 'click' / 'enter' / 'field'. If context is "
    'insufficient, return {"text": "", "confidence": 0.0, "rationale": "..."}.'
)


def _build_prompt(
    *,
    context: str,
    input_html: str,
    for_attr: str,
    page_title: Optional[str],
) -> str:
    parts: list[str] = []
    if page_title:
        parts.append(f"Page title: {page_title}")
    parts.append(f"Associated input: {input_html}")
    if for_attr:
        parts.append(f'Label references input with id="{for_attr}"')
    if context:
        parts.append(f"Surrounding context: {context}")
    parts.append(
        'Return JSON: {"text": "<label text>", '
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


def _label_text(label: Tag) -> str:
    return (label.get_text(strip=True) or "").replace("\u00a0", "").strip()


def _locate_label(
    page_html: str, element_html: str
) -> tuple[Optional[Tag], str, str]:
    """Find the empty label matching element_html. Returns (label,
    surrounding_context_html_snippet, associated_input_html).
    """
    page_soup = BeautifulSoup(page_html, "html.parser")
    target_soup = BeautifulSoup(element_html, "html.parser")
    target = target_soup.find("label")
    if target is None:
        return None, "", ""

    target_for = (target.get("for") or "").strip()
    for label in page_soup.find_all("label"):
        if _label_text(label):
            continue
        if target_for:
            if (label.get("for") or "").strip() == target_for:
                return label, _context_snippet(label), _associated_input(label, page_soup)
        else:
            # Match on serialized attribute set as a fallback
            if str(label).strip() == str(target).strip():
                return label, _context_snippet(label), _associated_input(label, page_soup)

    return None, "", ""


def _context_snippet(label: Tag, max_chars: int = 300) -> str:
    parent = label.parent
    if parent is None:
        return ""
    text = parent.get_text(" ", strip=True)
    if len(text) > max_chars:
        text = text[: max_chars - 1].rstrip() + "…"
    return text


def _associated_input(label: Tag, page_soup: BeautifulSoup) -> str:
    """Return the HTML of the input this label is for, or empty."""
    for_attr = (label.get("for") or "").strip()
    if for_attr:
        match = page_soup.find(attrs={"id": for_attr})
        if match is not None:
            return str(match)[:200]
    # Label wraps the input
    inner = label.find(["input", "select", "textarea"])
    if inner is not None:
        return str(inner)[:200]
    return ""


def _clone_with_text(label: Tag, text: str) -> Tag:
    soup = BeautifulSoup(str(label), "html.parser")
    clone = soup.find("label")
    if clone is None:
        return label
    clone.append(text)
    return clone
