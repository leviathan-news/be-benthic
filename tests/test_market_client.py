"""Tests for LNClient market-matching methods (HTTP mocked)."""
import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO = Path(__file__).resolve().parent.parent
TEST_KEY = "0x" + "0" * 63 + "1"  # throwaway key (int 1); never used on-chain


def _load_ln_agent():
    spec = importlib.util.spec_from_file_location("ln_agent", REPO / "ln-agent.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["ln_agent"] = module
    spec.loader.exec_module(module)
    return module


def _client(module):
    """An LNClient with a mocked session, and a no-op auth refresh."""
    c = module.LNClient(TEST_KEY)
    c.session = MagicMock()
    c._refresh_if_stale = lambda: None  # skip auth
    return c


def _resp(status=200, payload=None, ok=None):
    r = MagicMock()
    r.status_code = status
    r.ok = ok if ok is not None else (200 <= status < 300)
    r.json.return_value = payload if payload is not None else {}
    r.text = ""
    return r


def test_get_market_queue_returns_articles():
    module = _load_ln_agent()
    c = _client(module)
    c.session.get.return_value = _resp(payload={"articles": [{"id": 1}, {"id": 2}], "count": 2})
    out = c.get_market_queue(limit=20)
    assert [a["id"] for a in out] == [1, 2]
    args, kwargs = c.session.get.call_args
    assert "/agent/queue/" in args[0]
    assert kwargs["params"]["needs_market"] == "true"
    assert kwargs["params"]["limit"] == 20


def test_get_market_queue_empty_on_error():
    module = _load_ln_agent()
    c = _client(module)
    c.session.get.return_value = _resp(status=500, ok=False)
    assert c.get_market_queue() == []


def test_get_open_markets_returns_results():
    module = _load_ln_agent()
    c = _client(module)
    c.session.get.return_value = _resp(payload={"results": [{"id": 9, "question": "Q?"}], "total": 1})
    out = c.get_open_markets()
    assert out[0]["id"] == 9
    args, kwargs = c.session.get.call_args
    assert "/predictions/markets/" in args[0]
    assert kwargs["params"]["status"] == "open"


def test_submit_market_decision_success():
    module = _load_ln_agent()
    c = _client(module)
    c.session.post.return_value = _resp(payload={"result": "skipped", "decision_id": 5})
    res = c.submit_market_decision(42, {"decision": "skip", "reason": "not market-worthy", "confidence": 0.0})
    assert res["ok"] is True
    assert res["status"] == 200
    args, kwargs = c.session.post.call_args
    assert args[0].endswith("/agent/market-match/42/")
    assert kwargs["json"]["decision"] == "skip"


def test_submit_market_decision_409_is_benign():
    module = _load_ln_agent()
    c = _client(module)
    c.session.post.return_value = _resp(status=409, ok=False, payload={"error": "already decided"})
    res = c.submit_market_decision(42, {"decision": "skip", "reason": "x"})
    assert res["ok"] is False
    assert res["benign"] is True   # 409 already-decided is benign; record locally.


def test_submit_market_decision_noop_is_benign():
    module = _load_ln_agent()
    c = _client(module)
    c.session.post.return_value = _resp(payload={"result": "noop", "related_market_id": 3})
    res = c.submit_market_decision(42, {"decision": "attach", "market_id": 3, "reason": "x"})
    assert res["ok"] is True
    assert res["benign"] is True   # 200 noop means the article already has a market.


def test_submit_market_decision_400_not_benign():
    module = _load_ln_agent()
    c = _client(module)
    c.session.post.return_value = _resp(status=400, ok=False, payload={"error": "bad"})
    res = c.submit_market_decision(42, {"decision": "attach", "market_id": 1, "reason": "x"})
    assert res["ok"] is False
    assert res["benign"] is False


def test_submit_article_includes_market_id_when_set():
    module = _load_ln_agent()
    c = _client(module)
    c.session.post.return_value = _resp(payload={"news": {"id": 77}})
    c.submit_article("https://e.com/a", "A headline", market_id=9)
    args, kwargs = c.session.post.call_args
    assert kwargs["json"]["market_id"] == 9


def test_submit_article_omits_market_id_when_none():
    module = _load_ln_agent()
    c = _client(module)
    c.session.post.return_value = _resp(payload={"news": {"id": 77}})
    c.submit_article("https://e.com/a", "A headline")
    args, kwargs = c.session.post.call_args
    assert "market_id" not in kwargs["json"]
