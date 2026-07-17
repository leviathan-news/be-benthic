"""Tests for env knobs, plus the pre-attach helper (cycle-wiring smoke)."""
import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _load_ln_agent():
    spec = importlib.util.spec_from_file_location("ln_agent", REPO / "ln-agent.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["ln_agent"] = module
    spec.loader.exec_module(module)
    return module


def test_flag_defaults_off(monkeypatch):
    monkeypatch.delenv("ENABLE_MARKET_MATCH", raising=False)
    module = _load_ln_agent()
    assert module.ENABLE_MARKET_MATCH is False


def test_flag_reads_env(monkeypatch):
    monkeypatch.setenv("ENABLE_MARKET_MATCH", "1")
    module = _load_ln_agent()
    assert module.ENABLE_MARKET_MATCH is True


def test_env_float_defaults_on_bad_value(monkeypatch):
    monkeypatch.setenv("MARKET_MATCH_ATTACH_MIN_CONFIDENCE", "not-a-float")
    module = _load_ln_agent()
    assert module.MARKET_MATCH_ATTACH_MIN_CONFIDENCE == 0.75


def test_env_float_reads_value(monkeypatch):
    monkeypatch.setenv("MARKET_MATCH_ATTACH_MIN_CONFIDENCE", "0.6")
    module = _load_ln_agent()
    assert module.MARKET_MATCH_ATTACH_MIN_CONFIDENCE == 0.6


def test_max_b_reads_env(monkeypatch):
    monkeypatch.setenv("MARKET_MATCH_MAX_B", "2500")
    module = _load_ln_agent()
    assert module.MARKET_MATCH_MAX_B == 2500


def test_preattach_returns_market_id_on_attach(monkeypatch):
    module = _load_ln_agent()
    monkeypatch.setattr(module, "match_market_for_article",
                        lambda a, m, allowed: {"decision": "attach", "market_id": 7,
                                               "reason": "x", "confidence": 0.9})
    mid = module._preattach_market_id("Headline", [], "src", "https://e.com",
                                      [{"id": 7, "question": "Q", "expires_at": None}])
    assert mid == 7


def test_preattach_returns_none_on_skip(monkeypatch):
    module = _load_ln_agent()
    monkeypatch.setattr(module, "match_market_for_article",
                        lambda a, m, allowed: {"decision": "skip", "reason": "x", "confidence": 0.0})
    mid = module._preattach_market_id("Headline", [], "src", "https://e.com",
                                      [{"id": 7, "question": "Q", "expires_at": None}])
    assert mid is None


def test_preattach_none_when_no_markets(monkeypatch):
    module = _load_ln_agent()
    called = {"n": 0}
    monkeypatch.setattr(module, "match_market_for_article",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1) or {"decision": "skip"})
    assert module._preattach_market_id("H", [], "s", "u", []) is None
    assert called["n"] == 0  # no markets -> matcher not called


def test_preattach_passes_attach_skip_only(monkeypatch):
    module = _load_ln_agent()
    seen = {}
    def _matcher(a, m, allowed):
        seen["allowed"] = allowed
        return {"decision": "skip", "reason": "x", "confidence": 0.0}
    monkeypatch.setattr(module, "match_market_for_article", _matcher)
    module._preattach_market_id("H", [], "s", "u", [{"id": 1, "question": "Q", "expires_at": None}])
    assert seen["allowed"] == ["attach", "skip"]
