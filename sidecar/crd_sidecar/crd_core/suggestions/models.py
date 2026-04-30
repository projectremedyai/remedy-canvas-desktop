from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel


class FixSuggestion(BaseModel):
    id: str
    issue_id: str
    page_id: str
    page_title: Optional[str] = None
    page_file_path: Optional[str] = None
    rule_id: str
    category: str
    original_html: str
    proposed_html: str
    original_text: str
    proposed_text: str
    rationale: str
    confidence: float
    # Optional category-specific hints the applier needs. For CLR001
    # carries {selector, property, new_color}; other categories can
    # extend without changing the model shape.
    metadata: Optional[dict[str, Any]] = None
