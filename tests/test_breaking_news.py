"""Tests for benthic-bot breaking-news reaction (gates, worker, prompts)."""

import importlib.util
import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from prompt_loader import load_prompt  # noqa: E402


def test_gate_template_renders():
    p = load_prompt("bot/breaking_news_gate",
                    headline="ETH ETF approved", origin="manual")
    assert "ETH ETF approved" in p
    assert "NOTABLE" in p and "SKIP" in p


def test_craft_template_renders_with_no_slop():
    block = load_prompt("_shared/no_ai_slop")
    p = load_prompt("bot/breaking_news", no_slop=block,
                    headline="ETH ETF approved",
                    url="https://leviathannews.xyz/news/eth-etf",
                    chat_context="(none)")
    assert "NO AI SLOP" in p           # shared block injected
    assert "https://leviathannews.xyz/news/eth-etf" in p


# ─── Bot worker (Task 7) ─────────────────────────────────────────────────────

def _load_bot_module():
    """Import benthic-bot.py under a Python-safe module name (hyphen in filename)."""
    os.environ.setdefault("BENTHIC_BOT_TOKEN", "test:stub-token-do-not-use")
    os.environ.setdefault("WALLET_PRIVATE_KEY", "")
    if "benthic_bot_bn_test" in sys.modules:
        return sys.modules["benthic_bot_bn_test"]
    spec = importlib.util.spec_from_file_location(
        "benthic_bot_bn_test", ROOT / "benthic-bot.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["benthic_bot_bn_test"] = mod
    spec.loader.exec_module(mod)
    return mod


def _insert_event(bot, news_id, headline, age_seconds=0):
    from datetime import datetime, timedelta, timezone
    ts = (datetime.now(timezone.utc) - timedelta(seconds=age_seconds)).isoformat()
    with bot._db() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO ws_events
               (event_type, news_id, slug, headline, date_posted, origin, raw, received_at)
               VALUES ('news.approved', ?, 'slug', ?, NULL, 'manual', NULL, ?)""",
            (news_id, headline, ts))
        conn.commit()


@pytest.fixture
def bot_env(monkeypatch, tmp_path):
    bot = _load_bot_module()
    # Point the module at a scratch DB and rebuild tables there.
    monkeypatch.setattr(bot, "DB_FILE", tmp_path / "bot.db")
    bot._ensure_chat_table()
    monkeypatch.setattr(bot, "ENABLE_WS_BREAKING_NEWS", True)
    monkeypatch.setattr(bot, "_last_breaking_news", 0.0)
    sent = []
    monkeypatch.setattr(bot, "send_message",
                        lambda chat_id, text, **kw: sent.append((chat_id, text)) or {"ok": True})
    monkeypatch.setattr(bot, "save_own_action", lambda *a, **k: None)
    return bot, sent


def test_breaking_news_disabled_is_noop(bot_env, monkeypatch):
    bot, sent = bot_env
    monkeypatch.setattr(bot, "ENABLE_WS_BREAKING_NEWS", False)
    _insert_event(bot, 1, "Huge news")
    monkeypatch.setattr(bot, "llm_ask",
                        lambda *a, **k: pytest.fail("LLM must not be called when disabled"))
    bot._check_breaking_news()
    assert sent == []


def test_breaking_news_stale_rows_consumed_without_llm(bot_env, monkeypatch):
    bot, sent = bot_env
    _insert_event(bot, 2, "Old news", age_seconds=3600)  # > BREAKING_NEWS_MAX_AGE
    monkeypatch.setattr(bot, "llm_ask",
                        lambda *a, **k: pytest.fail("LLM must not run on stale rows"))
    bot._check_breaking_news()
    assert sent == []
    assert bot._bot_get_unconsumed_ws_events() == []  # marked consumed


def test_breaking_news_own_article_skipped(bot_env, monkeypatch):
    bot, sent = bot_env
    _insert_event(bot, 3, "Our own story")
    monkeypatch.setattr(bot, "_is_own_ln_article", lambda nid: nid == 3)
    monkeypatch.setattr(bot, "llm_ask",
                        lambda *a, **k: pytest.fail("LLM must not run on own article"))
    bot._check_breaking_news()
    assert sent == []
    assert bot._bot_get_unconsumed_ws_events() == []


def test_breaking_news_gate_skip_consumes_quietly(bot_env, monkeypatch):
    bot, sent = bot_env
    _insert_event(bot, 4, "Mildly interesting")
    monkeypatch.setattr(bot, "llm_ask", lambda *a, **k: "SKIP")
    bot._check_breaking_news()
    assert sent == []
    assert bot._bot_get_unconsumed_ws_events() == []
    assert bot._last_breaking_news == 0.0  # gate-skip must NOT consume the rate budget


def test_breaking_news_notable_sends_once_and_stamps(bot_env, monkeypatch):
    bot, sent = bot_env
    _insert_event(bot, 5, "SEC approves the thing")
    responses = iter(["NOTABLE", "Big regulatory move.\nhttps://leviathannews.xyz/news/slug"])
    monkeypatch.setattr(bot, "llm_ask", lambda *a, **k: next(responses))
    bot._check_breaking_news()
    assert len(sent) == 1
    assert sent[0][0] == bot.WS_NEWS_CHAT_ID
    assert bot._last_breaking_news > 0
    # Second run: nothing unconsumed, no send
    bot._check_breaking_news()
    assert len(sent) == 1


def test_breaking_news_rate_cap_blocks_second_send(bot_env, monkeypatch):
    bot, sent = bot_env
    _insert_event(bot, 6, "First big story")
    responses = iter(["NOTABLE", "take one\nlink"])
    monkeypatch.setattr(bot, "llm_ask", lambda *a, **k: next(responses))
    bot._check_breaking_news()
    assert len(sent) == 1

    _insert_event(bot, 7, "Second big story")
    monkeypatch.setattr(bot, "llm_ask",
                        lambda *a, **k: pytest.fail("rate cap must gate before LLM"))
    bot._check_breaking_news()          # inside BREAKING_NEWS_MIN_INTERVAL
    assert len(sent) == 1
    # 7 stays queued for the next allowed window (freshness decides later)
    assert [r["news_id"] for r in bot._bot_get_unconsumed_ws_events()] == [7]


def test_breaking_news_gate_uses_classification_tier(bot_env, monkeypatch):
    """The gate must select the classification TIER, never a provider-specific
    model name. Regression (live 2026-07-03): model="sonnet" overrides the
    Codex tier preset, so codex ran with `-m sonnet` — OpenAI rejects it, the
    Claude fallback is down (401), the chain exhausts, and 100% of gate calls
    fail-closed to SKIP without ever evaluating an article."""
    bot, sent = bot_env
    _insert_event(bot, 9, "Tier check story")
    calls = []

    def capture(prompt, *a, **k):
        calls.append(k)
        return "SKIP"

    monkeypatch.setattr(bot, "llm_ask", capture)
    bot._check_breaking_news()
    assert calls, "expected the gate to reach its LLM call"
    gate_kwargs = calls[0]
    assert gate_kwargs.get("tier") == "classification"
    assert gate_kwargs.get("model") is None
    assert gate_kwargs.get("effort") is None


def test_breaking_news_craft_skip_no_message(bot_env, monkeypatch):
    bot, sent = bot_env
    _insert_event(bot, 8, "Notable but nothing to add")
    responses = iter(["NOTABLE", "SKIP"])
    monkeypatch.setattr(bot, "llm_ask", lambda *a, **k: next(responses))
    bot._check_breaking_news()
    assert sent == []
    assert bot._bot_get_unconsumed_ws_events() == []
