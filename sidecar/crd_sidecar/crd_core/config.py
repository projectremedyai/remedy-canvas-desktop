"""Sidecar settings shim.

The Canvas Remedy-LTI codebase uses a dense Pydantic Settings class driven by env vars
and dotenv. The desktop sidecar needs only a small subset — just enough for
the Ollama vision client and the AI scheduler — so we inline it here rather
than vendoring the whole lti_app.config surface (which pulls in Canvas LTI,
Postgres, Firebase, etc.).

All values have env-var overrides so the Tauri shell can point the sidecar
at its bundled Ollama without recompiling Python.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True)
class Settings:
    # --- Ollama ---
    # Base URL of the Ollama OpenAI-compatible endpoint. In the packaged
    # app the Rust shell spawns Ollama on a free local port and sets this.
    ollama_base_url: str = "http://127.0.0.1:11434/v1"
    ollama_api_key: str = "ollama"  # placeholder; Ollama ignores it

    # Model tags. Keep them configurable — users can swap in any Ollama
    # model without rebuilding. Default to qwen3.5:4b, which is multimodal
    # (text + image) in a single 3.4 GB download, 256K context. Pulled from
    # ollama.com/library/qwen3.5:4b on first launch.
    ollama_text_model: str = "qwen3.5:4b"
    ollama_vision_model: str = "qwen3.5:4b"

    # KV cache context length passed to Ollama via `options.num_ctx` on every
    # chat completion. The Rust shell detects installed RAM and sets this
    # at sidecar spawn time: 8k (≤8 GB), 32k (9–16 GB), 64k (17–32 GB),
    # 128k (33–64 GB), 256k (65+ GB). 0 means "don't pass it" — let Ollama
    # use its default (which is whatever the Modelfile specifies, normally
    # 4k or 8k). Users can override at runtime via CRD_OLLAMA_NUM_CTX.
    ollama_num_ctx: int = 0

    # --- AI scheduler ---
    # Caps on concurrent Ollama calls so we don't overwhelm local inference.
    ai_max_concurrency: int = 2
    ai_per_run_cap: int = 4

    # --- Retries ---
    ai_max_retries: int = 3
    ai_base_retry_delay: float = 1.0


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(
        ollama_base_url=os.environ.get(
            "CRD_OLLAMA_BASE_URL", Settings.ollama_base_url
        ),
        ollama_api_key=os.environ.get(
            "CRD_OLLAMA_API_KEY", Settings.ollama_api_key
        ),
        ollama_text_model=os.environ.get(
            "CRD_OLLAMA_TEXT_MODEL", Settings.ollama_text_model
        ),
        ollama_vision_model=os.environ.get(
            "CRD_OLLAMA_VISION_MODEL", Settings.ollama_vision_model
        ),
        ai_max_concurrency=_env_int(
            "CRD_AI_MAX_CONCURRENCY", Settings.ai_max_concurrency
        ),
        ai_per_run_cap=_env_int(
            "CRD_AI_PER_RUN_CAP", Settings.ai_per_run_cap
        ),
        ai_max_retries=_env_int(
            "CRD_AI_MAX_RETRIES", Settings.ai_max_retries
        ),
        ai_base_retry_delay=_env_float(
            "CRD_AI_BASE_RETRY_DELAY", Settings.ai_base_retry_delay
        ),
        ollama_num_ctx=_env_int(
            "CRD_OLLAMA_NUM_CTX", Settings.ollama_num_ctx
        ),
    )
