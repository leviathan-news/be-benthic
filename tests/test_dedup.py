"""Tests for the cross-path dedup normalization and source blocklist.

Bug regression tests:
- _normalize_for_dedup / _content_key: covers the 2026-05-21 squid-digest
  double-reply where Telegram delivered "[photo] [photo] 🦑 SQUID DIGEST 📰"
  and the agent-chat API delivered "＜b＞🦑 SQUID DIGEST 📰＜/b＞" — different
  formatting, same content, must dedupe.
- is_blocked_source: covers the 2026-05-17 zine.live Wilder-World post that
  slipped through the eval prompt and reached LN as article 244050.
"""

import importlib.util
import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


def _load_bot_module():
    """Import benthic-bot.py without running its bottom-of-file poll() loop.
    The file uses a hyphen so a normal import doesn't work; load it by path
    and rely on `if __name__ == '__main__'` to guard the poll loop."""
    # Stub credentials so module-level reads don't bomb on a CI box.
    os.environ.setdefault("BENTHIC_BOT_TOKEN", "test:stub-token-do-not-use")
    os.environ.setdefault("WALLET_PRIVATE_KEY", "")
    if "benthic_bot_under_test" in sys.modules:
        return sys.modules["benthic_bot_under_test"]
    spec = importlib.util.spec_from_file_location(
        "benthic_bot_under_test", ROOT / "benthic-bot.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["benthic_bot_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


# ─── _normalize_for_dedup / _content_key ─────────────────────────────────────


def test_normalize_strips_leading_photo_prefix():
    bot = _load_bot_module()
    assert bot._normalize_for_dedup("[photo] hi") == "hi"
    assert bot._normalize_for_dedup("[photo] [photo] 🦑 SQUID DIGEST 📰") \
        == "🦑 SQUID DIGEST 📰"


def test_normalize_strips_document_prefix():
    bot = _load_bot_module()
    assert bot._normalize_for_dedup("[document: spec.md] @Benthic_Bot review please") \
        == "@Benthic_Bot review please"


def test_normalize_strips_fullwidth_html_tags():
    bot = _load_bot_module()
    assert bot._normalize_for_dedup("＜b＞hello＜/b＞") == "hello"
    assert bot._normalize_for_dedup("＜b＞🦑 SQUID DIGEST 📰＜/b＞\n＜i＞May 21, 2026＜/i＞") \
        == "🦑 SQUID DIGEST 📰 May 21, 2026"


def test_normalize_strips_ascii_html_tags():
    bot = _load_bot_module()
    assert bot._normalize_for_dedup("<b>hello</b>") == "hello"


def test_normalize_collapses_whitespace():
    bot = _load_bot_module()
    assert bot._normalize_for_dedup("a   b\n\nc") == "a b c"


def test_normalize_empty_input():
    bot = _load_bot_module()
    assert bot._normalize_for_dedup("") == ""
    assert bot._normalize_for_dedup(None) == ""


def test_cross_path_keys_match_for_squid_digest():
    """The original bug: Telegram '[photo] [photo] ...' and API '＜b＞...＜/b＞'
    must produce the SAME text-only content key so cross-path dedup catches it."""
    bot = _load_bot_module()
    telegram = "[photo] [photo] 🦑 SQUID DIGEST 📰\nMay 21, 2026\nCurve, WalletConnect..."
    api = "＜b＞🦑 SQUID DIGEST 📰＜/b＞\n＜i＞May 21, 2026＜/i＞\nCurve, WalletConnect..."
    assert bot._content_key(0, telegram) == bot._content_key(0, api)


def test_sender_keyed_keys_preserve_media_distinction():
    """Same user, same caption, different media should still produce different
    sender-keyed content keys — preserves the c3ec1bb behavior."""
    bot = _load_bot_module()
    plain = "@Benthic_Bot"
    with_doc = "[document: spec.md] @Benthic_Bot"
    assert bot._content_key(12345, plain) != bot._content_key(12345, with_doc)


def test_sender_keyed_key_normalization_only_for_zero_sender():
    """Non-zero sender keeps raw text; sender=0 normalizes. Verifies the gate."""
    bot = _load_bot_module()
    raw = "[photo] hello"
    # Non-zero: raw text hashed.
    assert bot._content_key(42, raw) == hash((42, raw[:200]))
    # Zero: normalized text hashed.
    assert bot._content_key(0, raw) == hash((0, "hello"[:200]))


# ─── TTL-windowed content dedup ──────────────────────────────────────────────


def test_content_dedup_hit_within_ttl():
    bot = _load_bot_module()
    bot._content_responded.clear()
    k = bot._content_key(0, "Review it")
    bot._mark_content_responded(k)
    assert bot._content_seen_recently(k) is True


def test_content_dedup_allows_after_ttl():
    bot = _load_bot_module()
    bot._content_responded.clear()
    k = bot._content_key(0, "Review it")
    bot._mark_content_responded(k)
    bot._content_responded[k] = time.time() - bot._CONTENT_DEDUP_TTL - 1
    assert bot._content_seen_recently(k) is False


def test_content_dedup_regression_repeated_review_command():
    bot = _load_bot_module()
    bot._content_responded.clear()
    k = bot._content_key(0, "Review it")
    bot._mark_content_responded(k)
    bot._content_responded[k] = time.time() - bot._CONTENT_DEDUP_TTL - 1
    assert not bot._content_seen_recently(k)


def test_prune_content_dedup_drops_stale_keeps_fresh():
    bot = _load_bot_module()
    bot._content_responded.clear()
    stale_key = bot._content_key(0, "old")
    fresh_key = bot._content_key(0, "fresh")
    bot._content_responded[stale_key] = time.time() - bot._CONTENT_DEDUP_TTL - 1
    bot._mark_content_responded(fresh_key)

    bot._prune_content_dedup()

    assert stale_key not in bot._content_responded
    assert fresh_key in bot._content_responded


# ─── is_blocked_source ───────────────────────────────────────────────────────


def test_blocked_source_rejects_zine_live():
    import ln_agent_import as la  # imported below
    assert la.is_blocked_source("https://www.zine.live/open-world-multiplayer-is-live/")
    assert la.is_blocked_source("https://zine.live/foo")


def test_blocked_source_subdomain_match():
    import ln_agent_import as la
    assert la.is_blocked_source("https://sub.zine.live/path")


def test_blocked_source_does_not_overmatch_unrelated_hosts():
    import ln_agent_import as la
    assert not la.is_blocked_source("https://bitcoinmagazine.com/article")
    # 'badzine.live' must not match 'zine.live' — host must be exact or subdomain.
    assert not la.is_blocked_source("https://badzine.live/foo")


def test_blocked_source_handles_garbage_input():
    import ln_agent_import as la
    assert not la.is_blocked_source("")
    assert not la.is_blocked_source("not a url")
    assert not la.is_blocked_source(None)


def test_blocked_source_blocks_known_aggregators():
    import ln_agent_import as la
    for u in [
        "https://cryptopotato.com/foo",
        "https://www.einnews.com/pr_news/123",
        "https://u.today/article",
        "https://watcher.guru/news/xyz",
    ]:
        assert la.is_blocked_source(u), u


# ─── _env_int ─────────────────────────────────────────────────────────────────


def test_env_int_returns_default_when_unset(monkeypatch):
    import ln_agent_import as la
    monkeypatch.delenv("TEST_KNOB", raising=False)
    assert la._env_int("TEST_KNOB", 42) == 42


def test_env_int_returns_default_when_blank(monkeypatch):
    import ln_agent_import as la
    monkeypatch.setenv("TEST_KNOB", "   ")
    assert la._env_int("TEST_KNOB", 42) == 42


def test_env_int_parses_valid(monkeypatch):
    import ln_agent_import as la
    monkeypatch.setenv("TEST_KNOB", "168")
    assert la._env_int("TEST_KNOB", 42) == 168


def test_env_int_falls_back_on_garbage(monkeypatch):
    """Malformed env values must not crash the cycle — they fall back to default."""
    import ln_agent_import as la
    monkeypatch.setenv("TEST_KNOB", "168h")
    assert la._env_int("TEST_KNOB", 42) == 42
    monkeypatch.setenv("TEST_KNOB", "not-a-number")
    assert la._env_int("TEST_KNOB", 42) == 42


# Load ln-agent.py under a Python-safe name so the blocked-source tests above
# can import it. Done at module import time so pytest collection works.
def _load_agent_module():
    spec = importlib.util.spec_from_file_location(
        "ln_agent_import", ROOT / "ln-agent.py")
    mod = importlib.util.module_from_spec(spec)
    # Don't actually run the agent — just expose the helpers.
    # ln-agent.py defines run_loop() but only calls it from __main__.
    sys.modules["ln_agent_import"] = mod
    spec.loader.exec_module(mod)
    return mod


# Trigger the load now so the `import ln_agent_import` lines above succeed.
_load_agent_module()
