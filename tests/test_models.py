"""Agent model chain configuration: names, defaults and the legacy-name warning."""

import logging

import pytest

from adpilot.core import models as m

AGENT_ENV = ("AGENT_LLM_BEARER_TOKEN", "AGENT_LLM_TARGET_MODEL", "AGENT_LLM_FALLBACK_MODEL")
LEGACY_ENV = ("OPENROUTER_API_KEY", "LLM_BEARER_TOKEN", "LLM_TARGET_MODEL", "LLM_FALLBACK_MODEL")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in AGENT_ENV + LEGACY_ENV:
        monkeypatch.delenv(k, raising=False)


def test_defaults_are_the_measured_chain():
    # 2026-09-21 A/B: gemma-4 free 429s on a shared quota, so it is no longer the fallback.
    assert m.DEFAULT_PRIMARY == "inclusionai/ling-3.0-flash-vl:free"
    assert m.DEFAULT_FALLBACK == "poolside/laguna-xs-2.1:free"


def test_api_key_reads_the_agent_name(monkeypatch):
    assert m.api_key_from_env() is None
    monkeypatch.setenv("AGENT_LLM_BEARER_TOKEN", "k")
    assert m.api_key_from_env() == "k"


def test_legacy_names_are_ignored_with_a_warning(monkeypatch, caplog):
    monkeypatch.setenv("OPENROUTER_API_KEY", "old")
    with caplog.at_level(logging.WARNING):
        assert m.api_key_from_env() is None
    assert "AGENT_LLM_BEARER_TOKEN" in caplog.text and "OPENROUTER_API_KEY" in caplog.text


def test_legacy_name_beside_the_new_one_warns_but_uses_the_new_one(monkeypatch, caplog):
    monkeypatch.setenv("AGENT_LLM_BEARER_TOKEN", "new")
    monkeypatch.setenv("LLM_BEARER_TOKEN", "old")
    with caplog.at_level(logging.WARNING):
        assert m.api_key_from_env() == "new"
    assert "LLM_BEARER_TOKEN" in caplog.text


def test_model_names_come_from_the_agent_env(monkeypatch):
    monkeypatch.setenv("AGENT_LLM_BEARER_TOKEN", "k")
    monkeypatch.setenv("AGENT_LLM_TARGET_MODEL", "vendor/primary")
    monkeypatch.setenv("AGENT_LLM_FALLBACK_MODEL", "vendor/fallback")
    chain = m.build_model()
    assert [x.model_name for x in chain.models] == ["vendor/primary", "vendor/fallback"]


def test_empty_fallback_means_no_fallback(monkeypatch):
    monkeypatch.setenv("AGENT_LLM_BEARER_TOKEN", "k")
    monkeypatch.setenv("AGENT_LLM_FALLBACK_MODEL", "")
    model = m.build_model()
    assert not hasattr(model, "models") and model.model_name == m.DEFAULT_PRIMARY
