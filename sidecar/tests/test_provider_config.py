"""Unit tests for the Provider abstraction in crd_core.config."""

from __future__ import annotations

import pytest

from crd_sidecar.crd_core.config import Provider, Settings, get_settings


def test_default_provider_is_local_ollama():
    s = Settings()
    assert s.provider == Provider.LOCAL_OLLAMA


def test_local_ollama_resolves_to_loopback():
    s = Settings()
    assert s.resolved_base_url() == "http://127.0.0.1:11434/v1"
    assert s.resolved_text_model() == "gemma4:e4b"
    assert s.resolved_vision_model() == "gemma4:e4b"
    assert s.resolved_api_key() == "ollama"
    assert s.extra_headers() == {}


def test_ollama_cloud_resolves_to_ollama_com():
    s = Settings(provider=Provider.OLLAMA_CLOUD, provider_api_key="cloud-key")
    assert s.resolved_base_url() == "https://ollama.com/api"
    assert s.resolved_api_key() == "cloud-key"
    # Default cloud model when not explicitly set
    assert s.resolved_text_model() == "gpt-oss:120b"
    assert s.extra_headers() == {}


def test_openrouter_resolves_with_attribution_headers():
    s = Settings(provider=Provider.OPENROUTER, provider_api_key="sk-or-test")
    assert s.resolved_base_url() == "https://openrouter.ai/api/v1"
    assert s.resolved_api_key() == "sk-or-test"
    assert s.resolved_text_model() == "openai/gpt-4o"
    headers = s.extra_headers()
    assert "HTTP-Referer" in headers
    assert "X-Title" in headers
    assert headers["X-Title"] == "Remedy Canvas Desktop"


def test_provider_text_model_override_wins():
    s = Settings(
        provider=Provider.OPENROUTER,
        provider_api_key="sk-or-test",
        provider_text_model="anthropic/claude-opus-4-7",
    )
    assert s.resolved_text_model() == "anthropic/claude-opus-4-7"


def test_provider_vision_model_falls_back_to_text_when_unset():
    s = Settings(
        provider=Provider.OLLAMA_CLOUD,
        provider_api_key="cloud-key",
        provider_text_model="custom-text",
        # provider_vision_model intentionally left empty
    )
    # Fallback for cloud vision is the same as cloud text default
    assert s.resolved_vision_model() == "gpt-oss:120b"


def test_unknown_provider_string_falls_back_to_local():
    assert Provider.from_env(None) == Provider.LOCAL_OLLAMA
    assert Provider.from_env("") == Provider.LOCAL_OLLAMA
    assert Provider.from_env("nonexistent") == Provider.LOCAL_OLLAMA
    assert Provider.from_env("OPENROUTER") == Provider.OPENROUTER  # case-insensitive
    assert Provider.from_env(" openrouter ") == Provider.OPENROUTER  # whitespace-trimmed


def test_env_var_drives_get_settings(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CRD_PROVIDER", "ollama-cloud")
    monkeypatch.setenv("CRD_PROVIDER_API_KEY", "envkey")
    monkeypatch.setenv("CRD_PROVIDER_TEXT_MODEL", "gpt-oss:120b")
    get_settings.cache_clear()  # lru_cache from prior tests/imports
    try:
        s = get_settings()
        assert s.provider == Provider.OLLAMA_CLOUD
        assert s.provider_api_key == "envkey"
        assert s.provider_text_model == "gpt-oss:120b"
    finally:
        get_settings.cache_clear()  # reset for downstream tests
