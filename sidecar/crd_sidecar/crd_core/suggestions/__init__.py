"""LLM-assisted fix suggestions for accessibility issues that require
human judgment (link-text rewriting, alt-text refinement, etc.).

Suggestions are surfaced to the UI for accept/reject review rather than
applied automatically — the explicit goal is that wrong alt text is
worse than no alt text, and an unreviewed confident-wrong fix ships
accessibility regressions to students.
"""

from crd_sidecar.crd_core.suggestions.models import FixSuggestion
from crd_sidecar.crd_core.suggestions.alt_text import (
    apply_alt_text_suggestion,
    generate_alt_text_suggestion,
)
from crd_sidecar.crd_core.suggestions.contrast import (
    apply_contrast_suggestion,
    generate_contrast_suggestion,
)
from crd_sidecar.crd_core.suggestions.form_label import (
    apply_form_label_suggestion,
    generate_form_label_suggestion,
)
from crd_sidecar.crd_core.suggestions.link_text import (
    apply_link_text_suggestion,
    generate_link_text_suggestion,
)
from crd_sidecar.crd_core.suggestions.table_caption import (
    apply_table_caption_suggestion,
    generate_table_caption_suggestion,
    row_signature_for_original,
)

__all__ = [
    "FixSuggestion",
    "apply_alt_text_suggestion",
    "apply_contrast_suggestion",
    "apply_form_label_suggestion",
    "apply_link_text_suggestion",
    "apply_suggestion_to_page",
    "apply_table_caption_suggestion",
    "generate_alt_text_suggestion",
    "generate_contrast_suggestion",
    "generate_form_label_suggestion",
    "generate_link_text_suggestion",
    "generate_table_caption_suggestion",
]


def apply_suggestion_to_page(page_html: str, suggestion: FixSuggestion) -> str | None:
    """Apply one accepted FixSuggestion to a page's HTML.

    Returns the rewritten HTML on success, or None if the original
    content couldn't be located (page changed externally / suggestion
    is stale). Dispatches on rule_id — new categories register here.
    """
    from bs4 import BeautifulSoup

    if suggestion.rule_id == "LNK004":
        src = BeautifulSoup(suggestion.original_html, "html.parser").find("a")
        href = (src.get("href") if src else "") or ""
        return apply_link_text_suggestion(
            page_html,
            original_text=suggestion.original_text,
            proposed_text=suggestion.proposed_text,
            href=href,
        )

    if suggestion.rule_id == "IMG001":
        img = BeautifulSoup(suggestion.original_html, "html.parser").find("img")
        img_src = (img.get("src") if img else "") or ""
        return apply_alt_text_suggestion(
            page_html,
            src=img_src,
            proposed_alt=suggestion.proposed_text,
        )

    if suggestion.rule_id == "FORM003":
        label = BeautifulSoup(suggestion.original_html, "html.parser").find("label")
        for_attr = (label.get("for") if label else "") or ""
        return apply_form_label_suggestion(
            page_html,
            for_attr=for_attr,
            proposed_text=suggestion.proposed_text,
        )

    if suggestion.rule_id == "TBL003":
        row_sig = row_signature_for_original(suggestion.original_html)
        return apply_table_caption_suggestion(
            page_html,
            row_signature=row_sig,
            proposed_caption=suggestion.proposed_text,
        )

    if suggestion.rule_id == "CLR001":
        meta = suggestion.metadata or {}
        return apply_contrast_suggestion(
            page_html,
            selector=meta.get("selector") or "",
            css_property=meta.get("property") or "color",
            new_color=suggestion.proposed_text,
        )

    return None
