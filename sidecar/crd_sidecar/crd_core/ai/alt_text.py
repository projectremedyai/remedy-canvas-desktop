"""Single-image alt-text generation using the Ollama vision client.

Stripped-down port of Canvas Remedy-LTI's alt_text.py — the desktop sidecar doesn't
need the full multi-candidate / judge / cache apparatus for Phase 4. All
it needs is: given an image file, return a short accessible alt string.

Phase 5 can revisit if multi-candidate selection matters for quality.
"""

from __future__ import annotations

import base64
import mimetypes
from pathlib import Path

from crd_sidecar.crd_core.ai.prompt_library import (
    ALT_TEXT_SYSTEM_PROMPT,
    get_alt_text_generation_prompt,
)
from crd_sidecar.crd_core.ai.vision_client import OllamaVisionClient
from crd_sidecar.crd_core.config import get_settings


def _data_url_for(image_path: Path) -> str:
    mime, _ = mimetypes.guess_type(image_path.name)
    if mime is None:
        mime = "image/png"
    data = image_path.read_bytes()
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{b64}"


async def generate_alt_text_for_file(
    image_path: str | Path,
    *,
    context: str = "",
    model: str | None = None,
) -> str:
    """Generate a short alt-text description of the image at ``image_path``.

    ``context`` is optional surrounding HTML / surrounding text that helps
    the model describe the image in context (e.g. a caption). Kept empty
    by default for the simplest case.
    """
    path = Path(image_path).expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"image not found: {path}")

    client = OllamaVisionClient()
    chosen_model = model or client.get_primary_model()
    user_prompt = get_alt_text_generation_prompt(context=context)

    messages: list[dict] = [
        {"role": "system", "content": ALT_TEXT_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": user_prompt},
                {
                    "type": "image_url",
                    "image_url": {"url": _data_url_for(path)},
                },
            ],
        },
    ]

    result = await client.chat(
        model=chosen_model,
        messages=messages,
        timeout=get_settings().ai_base_retry_delay * 0 + 120.0,
    )
    if isinstance(result, list):
        # Tool-call mode wasn't asked for, so this should never happen;
        # defensive fallback.
        return ""
    return (result or "").strip().strip('"').strip()
