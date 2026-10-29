"""Regression tests for Benthic's sparse article-comment policy."""

from __future__ import annotations

import importlib.util
import inspect
import sys
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


ROOT = Path(__file__).parent.parent


def _load_agent_module():
    """Load ln-agent.py without starting its runtime loop."""
    name = "ln_agent_article_comment_policy_test"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, ROOT / "ln-agent.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def agent(tmp_path):
    """Provide an isolated policy database while reusing the loaded module."""
    mod = _load_agent_module()
    db = mod.AgentDB(db_path=tmp_path / "article-policy.db")
    try:
        yield mod, db
    finally:
        db.close()


def test_resolve_craft_headline_returns_only_url_and_headline(monkeypatch):
    mod = _load_agent_module()
    monkeypatch.setattr(
        mod,
        "claude_ask",
        lambda prompt: (
            "===URL===\nhttps://example.com/primary\n"
            "===HEADLINE===\nProtocol launches a permissionless lending market"
        ),
    )

    assert mod.resolve_craft_headline(
        "https://example.com/original", "source message"
    ) == (
        "https://example.com/primary",
        "Protocol launches a permissionless lending market",
    )


def test_resolve_craft_headline_empty_provider_result_keeps_url(monkeypatch):
    mod = _load_agent_module()
    monkeypatch.setattr(mod, "claude_ask", lambda prompt: "")

    assert mod.resolve_craft_headline(
        "https://example.com/original", "source message"
    ) == ("https://example.com/original", "")


def test_resolve_craft_headline_discards_legacy_tldr_suffix(monkeypatch):
    mod = _load_agent_module()
    monkeypatch.setattr(
        mod,
        "claude_ask",
        lambda prompt: (
            "===URL===\nhttps://example.com/primary\n"
            "===HEADLINE===\nProtocol launches a permissionless lending market\n"
            "===TLDR===\nThis legacy summary must never become the headline."
        ),
    )

    assert mod.resolve_craft_headline(
        "https://example.com/original", "source message"
    ) == (
        "https://example.com/primary",
        "Protocol launches a permissionless lending market",
    )


def test_db_identifies_articles_posted_by_benthic(agent):
    _mod, db = agent
    db.save_posted(
        "https://example.com/own", "Own headline", ln_article_id=71
    )

    assert db.was_article_posted_by_us(71) is True
    assert db.was_article_posted_by_us(72) is False


def test_db_returns_persisted_article_vote(agent):
    _mod, db = agent
    assert db.get_article_vote(81) is None
    db.save_article_vote(81, -1)
    assert db.get_article_vote(81) == -1


def test_rolling_budget_allows_three_and_rejects_fourth(agent):
    _mod, db = agent
    now = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)

    assert db.reserve_unsolicited_comment(1, 3, now) is True
    assert db.reserve_unsolicited_comment(2, 3, now) is True
    assert db.reserve_unsolicited_comment(3, 3, now) is True
    assert db.has_unsolicited_comment_capacity(3, now) is False
    assert db.reserve_unsolicited_comment(4, 3, now) is False


def test_rolling_budget_expires_after_24_hours(agent):
    _mod, db = agent
    start = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
    later = start + timedelta(hours=24, microseconds=1)
    assert db.reserve_unsolicited_comment(11, 1, start) is True

    assert db.has_unsolicited_comment_capacity(1, later) is True
    assert db.reserve_unsolicited_comment(12, 1, later) is True


def test_zero_budget_disables_reservations(agent):
    _mod, db = agent
    assert db.has_unsolicited_comment_capacity(0) is False
    assert db.reserve_unsolicited_comment(21, 0) is False


def test_release_only_deletes_unpublished_reservation(agent):
    _mod, db = agent
    now = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)
    assert db.reserve_unsolicited_comment(31, 3, now) is True
    db.release_unsolicited_comment(31)
    assert db.has_unsolicited_comment_capacity(1, now) is True

    assert db.reserve_unsolicited_comment(32, 3, now) is True
    db.confirm_unsolicited_comment(32, now)
    db.release_unsolicited_comment(32)
    row = db._execute(
        "SELECT published_at FROM unsolicited_article_comments WHERE ln_article_id = ?",
        (32,),
    ).fetchone()
    assert row is not None and row["published_at"] is not None


def test_craft_comment_normalizes_exact_skip(monkeypatch):
    mod = _load_agent_module()
    monkeypatch.setattr(mod, "claude_ask", lambda prompt: "  SKIP\n")

    assert mod.craft_comment("Headline", [], "") == ""


def test_comment_limit_default_is_three():
    mod = _load_agent_module()
    assert mod.ARTICLE_COMMENT_MAX_PER_24H == 3


def test_run_agent_phase_three_contains_no_yap_publication():
    mod = _load_agent_module()
    source = textwrap.dedent(inspect.getsource(mod.run_agent))

    assert ".post_yap(" not in source
    assert "tsunami promotion note" not in source.casefold()
    assert "Added TL;DR" not in source
