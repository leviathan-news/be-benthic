"""Tests for run_market_match_phase, plus _market_prefilter (mocked client/llm/db)."""
import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO = Path(__file__).resolve().parent.parent


def _load_ln_agent():
    spec = importlib.util.spec_from_file_location("ln_agent", REPO / "ln-agent.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["ln_agent"] = module
    spec.loader.exec_module(module)
    return module


def _db(module, tmp_path):
    return module.AgentDB(db_path=tmp_path / "t.db")


def _client_with(module, queue, markets):
    c = MagicMock()
    c.get_market_queue.return_value = queue
    c.get_open_markets.return_value = markets
    c.submit_market_decision.return_value = {"ok": True, "status": 200, "result": "skipped", "benign": False}
    return c


def test_phase_skips_when_flag_off(tmp_path):
    module = _load_ln_agent()
    module.ENABLE_MARKET_MATCH = False
    c = _client_with(module, [{"id": 1, "headline": "h"}], [])
    db = _db(module, tmp_path)
    try:
        module.run_market_match_phase(c, db)
        c.get_market_queue.assert_not_called()
    finally:
        db.close()


def test_phase_processes_each_article(monkeypatch, tmp_path):
    module = _load_ln_agent()
    module.ENABLE_MARKET_MATCH = True
    module.MARKET_MATCH_MAX_PER_CYCLE = 10
    monkeypatch.setattr(module, "_market_prefilter", lambda a: True)
    monkeypatch.setattr(module, "match_market_for_article",
                        lambda a, m, allowed: {"decision": "skip", "reason": "x", "confidence": 0.0})
    c = _client_with(module, [{"id": 1, "headline": "h1"}, {"id": 2, "headline": "h2"}], [])
    db = _db(module, tmp_path)
    try:
        module.run_market_match_phase(c, db)
        assert c.submit_market_decision.call_count == 2
        assert db.was_market_decided(1) and db.was_market_decided(2)
    finally:
        db.close()


def test_phase_respects_local_dedup(monkeypatch, tmp_path):
    module = _load_ln_agent()
    module.ENABLE_MARKET_MATCH = True
    module.MARKET_MATCH_MAX_PER_CYCLE = 10
    monkeypatch.setattr(module, "_market_prefilter", lambda a: True)
    monkeypatch.setattr(module, "match_market_for_article",
                        lambda a, m, allowed: {"decision": "skip", "reason": "x", "confidence": 0.0})
    c = _client_with(module, [{"id": 1, "headline": "h1"}], [])
    db = _db(module, tmp_path)
    try:
        db.save_market_decision(1, "skip", None, 0.0)  # already decided
        module.run_market_match_phase(c, db)
        c.submit_market_decision.assert_not_called()
    finally:
        db.close()


def test_phase_prefilter_skips_without_matching(monkeypatch, tmp_path):
    module = _load_ln_agent()
    module.ENABLE_MARKET_MATCH = True
    module.MARKET_MATCH_MAX_PER_CYCLE = 10
    monkeypatch.setattr(module, "_market_prefilter", lambda a: False)  # not market-worthy
    called = {"n": 0}
    def _should_not_run(*a, **k):
        called["n"] += 1
        return {"decision": "skip", "reason": "x", "confidence": 0.0}
    monkeypatch.setattr(module, "match_market_for_article", _should_not_run)
    c = _client_with(module, [{"id": 1, "headline": "spam"}], [])
    db = _db(module, tmp_path)
    try:
        module.run_market_match_phase(c, db)
        assert called["n"] == 0                       # matcher never called
        c.submit_market_decision.assert_called_once()  # but a skip is still recorded
        sent = c.submit_market_decision.call_args[0][1]
        assert sent["decision"] == "skip"
    finally:
        db.close()


def test_phase_respects_per_cycle_cap(monkeypatch, tmp_path):
    module = _load_ln_agent()
    module.ENABLE_MARKET_MATCH = True
    module.MARKET_MATCH_MAX_PER_CYCLE = 2
    monkeypatch.setattr(module, "_market_prefilter", lambda a: True)
    monkeypatch.setattr(module, "match_market_for_article",
                        lambda a, m, allowed: {"decision": "skip", "reason": "x", "confidence": 0.0})
    queue = [{"id": i, "headline": f"h{i}"} for i in range(5)]
    c = _client_with(module, queue, [])
    db = _db(module, tmp_path)
    try:
        module.run_market_match_phase(c, db)
        assert c.submit_market_decision.call_count == 2  # capped
    finally:
        db.close()


def test_phase_one_bad_article_does_not_abort(monkeypatch, tmp_path):
    module = _load_ln_agent()
    module.ENABLE_MARKET_MATCH = True
    module.MARKET_MATCH_MAX_PER_CYCLE = 10
    monkeypatch.setattr(module, "_market_prefilter", lambda a: True)
    def _matcher(a, m, allowed):
        if a["id"] == 1:
            raise RuntimeError("boom")
        return {"decision": "skip", "reason": "x", "confidence": 0.0}
    monkeypatch.setattr(module, "match_market_for_article", _matcher)
    c = _client_with(module, [{"id": 1, "headline": "h1"}, {"id": 2, "headline": "h2"}], [])
    db = _db(module, tmp_path)
    try:
        module.run_market_match_phase(c, db)
        # Article 2 is still processed, despite article 1 raising.
        assert c.submit_market_decision.call_count == 1
        assert db.was_market_decided(2)
    finally:
        db.close()


def test_prefilter_passes_substantive_headline(monkeypatch):
    module = _load_ln_agent()
    monkeypatch.setattr(module, "llm_ask", lambda *a, **k: "yes")
    assert module._market_prefilter({"id": 1, "headline": "SEC to rule on spot ETH ETF by July"}) is True


def test_prefilter_fails_open_on_empty(monkeypatch):
    module = _load_ln_agent()
    monkeypatch.setattr(module, "llm_ask", lambda *a, **k: "")
    # Empty output -> fail open (let the full matcher decide).
    assert module._market_prefilter({"id": 1, "headline": "anything"}) is True


def test_prefilter_drops_on_no(monkeypatch):
    module = _load_ln_agent()
    monkeypatch.setattr(module, "llm_ask", lambda *a, **k: "no")
    assert module._market_prefilter({"id": 1, "headline": "My favourite memecoin vibes"}) is False


# ===========================================================================
# Robustness fixes (code-review follow-ups)
# ===========================================================================
def test_phase_transient_failure_not_recorded(monkeypatch, tmp_path):
    """FIX B: a transient submit failure (status 0) must NOT be recorded locally,
    so the article is retried on the next cycle instead of being silently dropped."""
    module = _load_ln_agent()
    module.ENABLE_MARKET_MATCH = True
    module.MARKET_MATCH_MAX_PER_CYCLE = 10
    monkeypatch.setattr(module, "_market_prefilter", lambda a: True)
    monkeypatch.setattr(module, "match_market_for_article",
                        lambda a, m, allowed: {"decision": "skip", "reason": "x", "confidence": 0.0})
    c = _client_with(module, [{"id": 7, "headline": "h7"}], [])
    # submit_market_decision exception path returns status 0 (timeout/network).
    c.submit_market_decision.return_value = {"ok": False, "status": 0, "result": "boom", "benign": False}
    db = _db(module, tmp_path)
    try:
        module.run_market_match_phase(c, db)
        # Attempted once, but NOT recorded — so a later cycle can retry it.
        c.submit_market_decision.assert_called_once()
        assert db.was_market_decided(7) is False
    finally:
        db.close()


def test_phase_client_error_400_is_recorded(monkeypatch, tmp_path):
    """FIX B guard: a deterministic 4xx (e.g. 400) IS recorded locally, since
    re-submitting the same decision would just recur."""
    module = _load_ln_agent()
    module.ENABLE_MARKET_MATCH = True
    module.MARKET_MATCH_MAX_PER_CYCLE = 10
    monkeypatch.setattr(module, "_market_prefilter", lambda a: True)
    monkeypatch.setattr(module, "match_market_for_article",
                        lambda a, m, allowed: {"decision": "skip", "reason": "x", "confidence": 0.0})
    c = _client_with(module, [{"id": 8, "headline": "h8"}], [])
    c.submit_market_decision.return_value = {"ok": False, "status": 400, "result": "bad", "benign": False}
    db = _db(module, tmp_path)
    try:
        module.run_market_match_phase(c, db)
        c.submit_market_decision.assert_called_once()
        assert db.was_market_decided(8) is True
    finally:
        db.close()


def test_prefilter_load_prompt_failure_fails_open(monkeypatch):
    """FIX A: a prompt-build error (e.g. missing template) must fail OPEN —
    return True so the full matcher still runs — not raise."""
    module = _load_ln_agent()

    def _boom(*a, **k):
        raise FileNotFoundError("agent/market_prefilter")

    monkeypatch.setattr(module, "load_prompt", _boom)
    # llm_ask must never be reached; if it is, surface a clear failure.
    monkeypatch.setattr(
        module, "llm_ask",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("llm_ask should not run when load_prompt fails")),
    )
    result = module._market_prefilter(
        {"id": 1, "headline": "A real, substantive crypto headline"})
    assert result is True
