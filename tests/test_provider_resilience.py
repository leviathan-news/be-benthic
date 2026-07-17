"""Tests for the bot's periodic provider-breaker reset.

Context (2026-07-07/08 outage): 3 consecutive Codex failures on the evening of
07-06 (spark quota exhaustion window) latched the bot's Codex circuit breaker,
and Claude's breaker was already latched (401 since 06-23). benthic-bot never
called reset_failures() — unlike ln-agent, which resets at every cycle start —
so every later LLM call skipped both providers instantly (0.0s empty responses)
until a manual restart ~2 days later. The bot was silent on Telegram the whole
time. The periodic reset gives transient failure bursts a retry path while
preserving quota cooldowns (reset_failures clears counts, not cooldowns).
"""

import importlib.util
import os
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


def _load_bot_module():
    os.environ.setdefault("BENTHIC_BOT_TOKEN", "test:stub-token-do-not-use")
    os.environ.setdefault("WALLET_PRIVATE_KEY", "")
    if "benthic_bot_resilience_test" in sys.modules:
        return sys.modules["benthic_bot_resilience_test"]
    spec = importlib.util.spec_from_file_location(
        "benthic_bot_resilience_test", ROOT / "benthic-bot.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["benthic_bot_resilience_test"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_breaker_reset_fires_after_interval(monkeypatch):
    bot = _load_bot_module()
    calls = []
    monkeypatch.setattr(bot._provider_chain, "reset_failures",
                        lambda: calls.append(1))
    monkeypatch.setattr(bot, "_last_breaker_reset", 0.0)  # interval long elapsed
    bot._maybe_reset_provider_breakers()
    assert calls == [1]


def test_breaker_reset_gated_inside_interval(monkeypatch):
    bot = _load_bot_module()
    calls = []
    monkeypatch.setattr(bot._provider_chain, "reset_failures",
                        lambda: calls.append(1))
    monkeypatch.setattr(bot, "_last_breaker_reset", 0.0)
    bot._maybe_reset_provider_breakers()   # fires and stamps the time
    bot._maybe_reset_provider_breakers()   # inside the interval — must be a no-op
    assert calls == [1]


def test_poll_loop_wires_the_reset():
    """Wiring tripwire: the reset must run inside poll()'s loop, next to the
    other periodic hooks — a helper nobody calls is the same outage again."""
    import inspect
    bot = _load_bot_module()
    assert "_maybe_reset_provider_breakers()" in inspect.getsource(bot.poll)
