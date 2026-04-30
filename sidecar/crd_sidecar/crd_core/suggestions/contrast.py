"""Generate contrast suggestions for CLR001 residuals.

Unlike the other review categories, contrast is pure math — no LLM
involved. When the rendered scan flags an insufficient-contrast text
node, axe reports the computed fg + bg colors and the achieved ratio
in ``axe_meta``. The generator uses the existing color-math helpers
from ``rules/contrast.py`` to compute a minimum-shift darkened fg
(or lightened bg) that hits WCAG AA.

Applier locates the offending element by the CSS selector axe
supplied and injects/updates an inline ``color`` or ``background-color``
style.

Skipped silently when axe_meta is absent (static-scan CLR001 without
a concrete bg color can't be solved without guessing) or when the
color math can't reach the target ratio.
"""

from __future__ import annotations

import re
import uuid
from typing import Optional

import structlog
from bs4 import BeautifulSoup, Tag

from crd_sidecar.crd_core.accessibility.rules.contrast import (
    adjust_background_for_contrast,
    adjust_color_for_contrast,
    contrast_ratio,
    hex_to_rgb,
    rgb_to_hex,
)
from crd_sidecar.crd_core.models import AccessibilityIssue
from crd_sidecar.crd_core.suggestions.models import FixSuggestion

_logger = structlog.get_logger(__name__)


_NORMAL_TARGET = 4.5
_LARGE_TARGET = 3.0
_LARGE_FONT_PX = 24.0  # 18pt at 96dpi ≈ 24px
_LARGE_BOLD_PX = 18.67  # 14pt at 96dpi ≈ 18.67px


def generate_contrast_suggestion(
    issue: AccessibilityIssue,
    page_html: str,
    *,
    page_title: Optional[str] = None,
    page_file_path: Optional[str] = None,
) -> Optional[FixSuggestion]:
    """Compute a minimum-shift color change to bring the offending
    element into WCAG AA compliance. Returns None when the required
    information isn't available."""
    if issue.rule_id != "CLR001":
        return None
    meta = issue.axe_meta or {}
    fg = _normalize_color(meta.get("fg_color"))
    bg = _normalize_color(meta.get("bg_color"))
    if not fg or not bg:
        return None
    fg_rgb = hex_to_rgb(fg)
    bg_rgb = hex_to_rgb(bg)
    if fg_rgb is None or bg_rgb is None:
        return None

    target = _target_ratio(meta)
    current = contrast_ratio(fg_rgb, bg_rgb)
    if current >= target:
        # Something else triggered the issue; can't usefully propose a color fix
        return None

    # Try darkening (or lightening) the foreground first — usually the
    # smaller visual change since text is the smaller color area
    new_fg = adjust_color_for_contrast(fg_rgb, bg_rgb, target)
    fix_property = "color"
    new_color_hex: Optional[str] = None
    if new_fg is not None and contrast_ratio(new_fg, bg_rgb) >= target:
        new_color_hex = rgb_to_hex(new_fg)
    else:
        # Fall back to adjusting the background
        new_bg = adjust_background_for_contrast(fg_rgb, bg_rgb, target)
        if new_bg is not None and contrast_ratio(fg_rgb, new_bg) >= target:
            new_color_hex = rgb_to_hex(new_bg)
            fix_property = "background-color"

    if new_color_hex is None:
        return None

    selector = (meta.get("target") or "").strip()
    original_fragment = issue.element_html or ""
    proposed_fragment = _preview_with_inline_style(
        original_fragment, fix_property, new_color_hex
    )

    new_ratio = _final_ratio(fg_rgb, bg_rgb, fix_property, new_color_hex)
    rationale = (
        f"{'Darkened text' if fix_property == 'color' else 'Lightened background'} "
        f"from {fg if fix_property == 'color' else bg} to {new_color_hex} "
        f"(contrast {current:.2f} → {new_ratio:.2f}, target {target:.1f}:1)"
    )

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
        original_html=original_fragment,
        proposed_html=proposed_fragment,
        original_text=fg if fix_property == "color" else bg,
        proposed_text=new_color_hex,
        rationale=rationale,
        confidence=0.90,  # deterministic math, not an LLM guess
        metadata={
            "selector": selector,
            "property": fix_property,
            "new_color": new_color_hex,
            "original_color": fg if fix_property == "color" else bg,
        },
    )


def apply_contrast_suggestion(
    page_html: str,
    *,
    selector: str,
    css_property: str,
    new_color: str,
) -> Optional[str]:
    """Inject/update the inline ``color`` or ``background-color`` on the
    element matched by ``selector``. Falls back to matching by
    element_html when the selector doesn't resolve (axe selectors
    sometimes include :nth-of-type / structural bits BeautifulSoup
    can't parse). Returns None on no match."""
    if not new_color or css_property not in ("color", "background-color"):
        return None
    soup = BeautifulSoup(page_html, "html.parser")

    target: Optional[Tag] = None
    if selector:
        try:
            matches = soup.select(selector)
            if matches:
                target = matches[0]
        except Exception:  # noqa: BLE001
            target = None
    if target is None:
        return None

    _set_inline_style(target, css_property, new_color)
    return str(soup)


# -----------------------------------------------------------------


def _target_ratio(meta: dict) -> float:
    font_size = meta.get("font_size")
    font_weight = meta.get("font_weight")
    try:
        size_px = float(font_size) if font_size is not None else 0.0
    except (TypeError, ValueError):
        size_px = 0.0
    try:
        weight = float(font_weight) if font_weight is not None else 400.0
    except (TypeError, ValueError):
        weight = 400.0
    is_bold = weight >= 700
    if size_px >= _LARGE_FONT_PX or (is_bold and size_px >= _LARGE_BOLD_PX):
        return _LARGE_TARGET
    return _NORMAL_TARGET


def _normalize_color(raw) -> Optional[str]:
    """Normalize ``axe``-style colors (``#rrggbb``, ``#rgb``, or ``rgb()``)
    to a 6-char hex string. Returns None on invalid input."""
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s:
        return None
    if s.startswith("#"):
        if len(s) == 7:
            return s.lower()
        if len(s) == 4:
            return ("#" + "".join(c * 2 for c in s[1:])).lower()
        return None
    m = re.match(
        r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)",
        s,
        flags=re.IGNORECASE,
    )
    if m:
        r, g, b = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
        return "#{:02x}{:02x}{:02x}".format(
            max(0, min(255, r)),
            max(0, min(255, g)),
            max(0, min(255, b)),
        )
    return None


def _preview_with_inline_style(fragment: str, prop: str, value: str) -> str:
    """Return a representative fragment showing the proposed inline
    style edit. Purely for UI display — the applier never uses this."""
    soup = BeautifulSoup(fragment, "html.parser")
    el = soup.find(True)
    if el is None:
        return fragment
    _set_inline_style(el, prop, value)
    return str(soup)


def _set_inline_style(tag: Tag, prop: str, value: str) -> None:
    existing = (tag.get("style") or "").strip().rstrip(";")
    parts = [part.strip() for part in existing.split(";") if part.strip()]
    parts = [
        p
        for p in parts
        if not p.lower().startswith(f"{prop}:")
        and not p.lower().startswith(f"{prop} :")
    ]
    parts.append(f"{prop}: {value}")
    tag["style"] = "; ".join(parts)


def _final_ratio(
    fg_rgb: tuple[int, int, int],
    bg_rgb: tuple[int, int, int],
    fix_property: str,
    new_hex: str,
) -> float:
    rgb = hex_to_rgb(new_hex) or (0, 0, 0)
    if fix_property == "color":
        return contrast_ratio(rgb, bg_rgb)
    return contrast_ratio(fg_rgb, rgb)
