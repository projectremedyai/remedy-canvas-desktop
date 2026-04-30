"""Generate alt-text suggestions for IMG001 residuals.

IMG001 fires on `<img>` elements missing accessible alt text. The main
remediation pass either uses the deterministic transformer fallback
(filename-derived alt) or, when the `generate_alt_text` toggle is on,
vision-model output that gets auto-applied. Residuals are images the
vision model couldn't confidently handle — or images that were never
processed because the toggle was off.

The review-assisted path runs the same vision model on each residual
image and surfaces the result as a FixSuggestion instead of applying
it. The user accepts or rejects per-image, preserving the "wrong alt
text is worse than no alt text" invariant.
"""

from __future__ import annotations

import re
import uuid
from typing import Optional

import structlog
from bs4 import BeautifulSoup, Tag

from crd_sidecar.crd_core.ai import alt_text as alt_text_module
from crd_sidecar.crd_core.models import AccessibilityIssue
from crd_sidecar.crd_core.suggestions.models import FixSuggestion
from crd_sidecar.imscc import IMSCCImageFetcher

_logger = structlog.get_logger(__name__)


_LOW_CONFIDENCE_PHRASES = (
    "unclear",
    "cannot determine",
    "i cannot",
    "unable to",
    "no image",
    "no visible",
    "decorative",  # if the model says decorative, low confidence for alt
)


async def generate_alt_text_suggestion(
    issue: AccessibilityIssue,
    page_html: str,
    fetcher: IMSCCImageFetcher,
    *,
    tmp_root: str,
    page_title: Optional[str] = None,
    page_file_path: Optional[str] = None,
) -> Optional[FixSuggestion]:
    """Produce a FixSuggestion for an IMG001 issue, or None on failure.

    Extracts the image from the IMSCC zip to ``tmp_root``, calls the
    vision model, and packages the result into a FixSuggestion. Fails
    open — unresolvable src values (external URLs, missing zip
    members), vision-client errors, and empty/unhelpful responses all
    return None rather than poisoning the batch.
    """
    if issue.rule_id != "IMG001":
        return None
    if not issue.element_html:
        return None

    src = _extract_src(issue.element_html)
    if not src:
        return None

    anchor = _locate_img(page_html, src)
    if anchor is None:
        return None

    zip_path = fetcher.resolve_src_to_zip_path(src)
    if zip_path is None:
        # External URL or unresolvable reference — no image bytes available.
        return None

    try:
        extracted = fetcher.extract_to(src, tmp_root)
    except Exception as exc:  # noqa: BLE001
        _logger.warning(
            "alt_text_suggestion_extract_failed",
            issue_id=issue.id,
            src=src,
            error=str(exc),
        )
        return None
    if extracted is None:
        return None

    try:
        alt = await alt_text_module.generate_alt_text_for_file(
            extracted,
            context=page_title or "",
        )
    except Exception as exc:  # noqa: BLE001
        _logger.warning(
            "alt_text_suggestion_llm_failed",
            issue_id=issue.id,
            src=src,
            error=str(exc),
        )
        return None

    alt = (alt or "").strip()
    if not alt:
        return None
    # Strip obvious preamble like "Alt text:" the model sometimes emits
    alt = re.sub(r"^(alt text|alt|description)[:\s-]+", "", alt, flags=re.IGNORECASE).strip()
    if not alt:
        return None

    confidence = _score_confidence(alt)
    proposed_anchor = _clone_with_alt(anchor, alt)

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
        original_html=str(anchor),
        proposed_html=str(proposed_anchor),
        original_text=(anchor.get("alt") or "").strip(),
        proposed_text=alt,
        rationale=f"Vision model described image at {src}",
        confidence=confidence,
    )


def apply_alt_text_suggestion(
    page_html: str,
    *,
    src: str,
    proposed_alt: str,
) -> Optional[str]:
    """Set the alt attribute on the first <img> matching ``src``.

    Returns updated page HTML, or None when no matching img is found
    (e.g., the page was edited out from under us).
    """
    if not proposed_alt or not src:
        return None
    soup = BeautifulSoup(page_html, "html.parser")
    norm_src = src.strip()
    for img in soup.find_all("img"):
        img_src = (img.get("src") or "").strip()
        if img_src == norm_src:
            img["alt"] = proposed_alt
            # Drop role="presentation" / role="none" so screen readers
            # actually announce the new alt text
            role = (img.get("role") or "").strip().lower()
            if role in {"presentation", "none"}:
                del img["role"]
            return str(soup)
    return None


def _extract_src(fragment: str) -> str:
    soup = BeautifulSoup(fragment, "html.parser")
    img = soup.find("img")
    if img is None:
        return ""
    return (img.get("src") or "").strip()


def _locate_img(page_html: str, src: str) -> Optional[Tag]:
    soup = BeautifulSoup(page_html, "html.parser")
    for img in soup.find_all("img"):
        if (img.get("src") or "").strip() == src:
            return img
    return None


def _clone_with_alt(img: Tag, alt: str) -> Tag:
    soup = BeautifulSoup(str(img), "html.parser")
    clone = soup.find("img")
    if clone is None:
        return img
    clone["alt"] = alt
    return clone


def _score_confidence(alt: str) -> float:
    """Cheap heuristic — real per-sample confidence would need the model
    to emit a score, which the existing alt-text prompt doesn't ask for.
    Length + hedge-phrase detection is a decent proxy:
    - Too short (<8 chars): likely unhelpful
    - Too long (>250 chars): probably rambling / uncertain
    - Contains low-confidence phrases: cap below threshold
    """
    length = len(alt)
    base = 0.75
    if length < 8:
        return 0.25
    if length > 250:
        base = 0.45
    elif 15 <= length <= 120:
        base = 0.80
    lower = alt.lower()
    if any(phrase in lower for phrase in _LOW_CONFIDENCE_PHRASES):
        base = min(base, 0.35)
    return round(base, 2)
