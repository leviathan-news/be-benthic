"""Tests for AgentDB.market_decisions — the Phase 6 dedup table."""
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


def test_unseen_article_not_decided(tmp_path):
    module = _load_ln_agent()
    db = module.AgentDB(db_path=tmp_path / "t.db")
    try:
        assert db.was_market_decided(123) is False
    finally:
        db.close()


def test_save_then_seen(tmp_path):
    module = _load_ln_agent()
    db = module.AgentDB(db_path=tmp_path / "t.db")
    try:
        db.save_market_decision(123, "attach", market_id=7, confidence=0.9)
        assert db.was_market_decided(123) is True
    finally:
        db.close()


def test_save_is_idempotent(tmp_path):
    module = _load_ln_agent()
    db = module.AgentDB(db_path=tmp_path / "t.db")
    try:
        db.save_market_decision(123, "attach", market_id=7, confidence=0.9)
        # A second save for the same news_id must not raise (INSERT OR IGNORE).
        db.save_market_decision(123, "skip", market_id=None, confidence=0.0)
        assert db.was_market_decided(123) is True
    finally:
        db.close()


def test_skip_decision_with_null_market(tmp_path):
    module = _load_ln_agent()
    db = module.AgentDB(db_path=tmp_path / "t.db")
    try:
        db.save_market_decision(456, "skip", market_id=None, confidence=0.0)
        assert db.was_market_decided(456) is True
    finally:
        db.close()
