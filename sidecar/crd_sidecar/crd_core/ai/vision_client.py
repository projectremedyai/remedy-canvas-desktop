"""Unified vision client interface for AI backends."""

from __future__ import annotations

import asyncio
import json as _json
from typing import Protocol

import openai
import structlog

from openai import AsyncOpenAI

from crd_sidecar.crd_core.config import get_settings
from crd_sidecar.crd_core.ai.ai_scheduler import (
    ai_run_slot,
    is_retryable_status,
    retry_delay,
    should_retry,
)

_logger = structlog.get_logger(__name__)


class VisionClient(Protocol):
    """Protocol for AI vision/chat backends."""

    async def chat(
        self,
        *,
        model: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        tool_choice: dict | str | None = None,
        run_id: str | None = None,
        timeout: float = 90.0,
        reasoning_effort: str = "none",
    ) -> str | list[dict]:
        """Send a chat completion request.

        Returns content string when tools is None, or a list of
        tool call dicts ``[{"name": ..., "arguments": ...}]`` when tools
        are provided.
        """
        ...

    def get_primary_model(self) -> str: ...
    def get_fallback_model(self) -> str: ...


class OllamaVisionClient:
    """Ollama via OpenAI-compatible endpoint with retry logic and Langfuse tracing."""

    def __init__(self) -> None:
        settings = get_settings()
        self._client = AsyncOpenAI(
            base_url=settings.resolved_base_url(),
            api_key=settings.resolved_api_key() or "placeholder",
            max_retries=0,  # we handle retries ourselves
            timeout=90.0,
        )
        self._extra_headers = settings.extra_headers()

    def get_primary_model(self) -> str:
        return get_settings().resolved_vision_model()

    def get_fallback_model(self) -> str:
        return get_settings().resolved_text_model()

    @staticmethod
    def _extract_tool_calls(completion: object) -> list[dict]:
        """Extract tool calls from a ChatCompletion response object."""
        message = completion.choices[0].message  # type: ignore[attr-defined]
        raw_calls = message.tool_calls or []
        results: list[dict] = []
        for call in raw_calls:
            fn = call.function
            args_str = fn.arguments or "{}"
            try:
                args = _json.loads(args_str) if isinstance(args_str, str) else args_str
            except _json.JSONDecodeError:
                args = {"raw": args_str}
            results.append({"name": fn.name or "", "arguments": args})
        # Fallback: if model returned content instead of tool_calls, wrap it
        if not results and message.content:
            results.append({
                "name": "fix_html_content",
                "arguments": {"fixed_html": message.content, "changes_made": []},
            })
        return results

    async def chat(
        self,
        *,
        model: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        tool_choice: dict | str | None = None,
        run_id: str | None = None,
        timeout: float = 90.0,
        reasoning_effort: str = "none",
    ) -> str | list[dict]:
        """Send a chat completion request to the Ollama OpenAI-compat endpoint.

        ``reasoning_effort`` defaults to ``"none"`` because all of Canvas Remedy-LTI's
        use cases (HTML transformation, image description, document conversion,
        OCR) want fast, direct responses -- not internal monologue. The
        Ollama Cloud thinking models (kimi-k2.5:cloud, gemma4:31b-cloud) have
        thinking ON by default, which on a real document chunk turned a 5s
        request into a 94s one (verified live A/B benchmark, 2026-04-06). The
        OpenAI-compat endpoint accepts ``"none" | "low" | "medium" | "high"``
        per Ollama's docs. Pass a non-default value if a caller actually
        wants reasoning traces.
        """
        kwargs: dict = {
            "model": model,
            "messages": messages,
            "timeout": timeout,
        }

        # reasoning_effort + num_ctx go via extra_body, not top-level kwargs
        extra_body: dict = {}
        if reasoning_effort:
            extra_body["reasoning_effort"] = reasoning_effort

        # Size the KV cache to match the host machine's memory tier. The Rust
        # shell detects installed RAM and sets CRD_OLLAMA_NUM_CTX;
        # the Ollama OpenAI-compat endpoint accepts native options via
        # `options.num_ctx` in extra_body. Skip when ollama_num_ctx==0 so
        # Ollama's Modelfile default wins (useful for dev + CI).
        settings = get_settings()
        if settings.ollama_num_ctx > 0:
            extra_body.setdefault("options", {})["num_ctx"] = settings.ollama_num_ctx

        if extra_body:
            kwargs["extra_body"] = extra_body

        if tools:
            kwargs["tools"] = tools
        if tool_choice:
            kwargs["tool_choice"] = tool_choice

        # Pass provider-specific headers (e.g. HTTP-Referer, X-Title for
        # OpenRouter). For LOCAL_OLLAMA the dict is empty and the SDK no-ops.
        kwargs["extra_headers"] = self._extra_headers

        for attempt in range(4):  # 1 initial + 3 retries
            async with ai_run_slot(run_id):
                try:
                    completion = await self._client.chat.completions.create(**kwargs)
                    if tools:
                        return self._extract_tool_calls(completion)
                    return (completion.choices[0].message.content or "").strip()

                except openai.APIStatusError as exc:
                    if is_retryable_status(exc.status_code) and should_retry(attempt):
                        delay = retry_delay(attempt)
                        _logger.warning(
                            "ollama_retry",
                            model=model,
                            status_code=exc.status_code,
                            delay=round(delay, 1),
                            attempt=attempt + 1,
                        )
                        await asyncio.sleep(delay)
                        continue
                    raise

                except (openai.APIConnectionError, openai.APITimeoutError) as exc:
                    if should_retry(attempt):
                        delay = retry_delay(attempt)
                        _logger.warning(
                            "ollama_retry",
                            model=model,
                            delay=round(delay, 1),
                            error=str(exc),
                        )
                        await asyncio.sleep(delay)
                        continue
                    raise

        return [] if tools else ""


def get_vision_client(backend: str | None = None) -> OllamaVisionClient:
    """Create a vision client (Ollama only)."""
    return OllamaVisionClient()
