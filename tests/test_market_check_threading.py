"""Tests for running Benthic's periodic market check off the poll loop."""

import importlib.util
import os
import sys
import threading
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


def _load_bot_module():
    """Import benthic-bot.py under a Python-safe module name for direct helper tests."""
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


@pytest.fixture
def bot():
    """Restore market-check globals so thread state cannot leak across tests."""
    mod = _load_bot_module()
    saved_last = mod._last_market_check
    if hasattr(mod, "_market_check_lock") and mod._market_check_lock.locked():
        mod._market_check_lock.release()
    yield mod
    for thread in threading.enumerate():
        if thread.name == "market-check":
            thread.join(timeout=1)
    mod._last_market_check = saved_last
    if hasattr(mod, "_market_check_lock") and mod._market_check_lock.locked():
        mod._market_check_lock.release()


def test_market_check_spawner_returns_without_waiting_for_check(bot, monkeypatch):
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    threads = []

    def held_check(recent_messages):
        """Block inside the fake check so the test can prove it runs in a thread."""
        threads.append(threading.current_thread())
        started.set()
        release.wait(timeout=2)
        finished.set()

    monkeypatch.setattr(bot, "_check_markets", held_check)
    bot._last_market_check = 0

    start = time.monotonic()
    bot._maybe_spawn_market_check([])
    elapsed = time.monotonic() - start

    try:
        assert elapsed < 0.5
        assert started.wait(timeout=0.5)
        assert not finished.is_set()
    finally:
        release.set()
        if threads:
            threads[0].join(timeout=1)


def test_market_check_spawner_is_single_flight(bot, monkeypatch):
    started = threading.Event()
    release = threading.Event()
    calls = 0
    threads = []

    def held_check(recent_messages):
        """Keep the first check active while a second spawn attempt is made."""
        nonlocal calls
        calls += 1
        threads.append(threading.current_thread())
        started.set()
        release.wait(timeout=2)

    monkeypatch.setattr(bot, "_check_markets", held_check)
    bot._last_market_check = 0

    bot._maybe_spawn_market_check([])
    try:
        assert started.wait(timeout=0.5)
        bot._maybe_spawn_market_check([])
        time.sleep(0.05)
        assert calls == 1
    finally:
        release.set()
        if threads:
            threads[0].join(timeout=1)


def test_market_check_spawner_respects_interval_throttle(bot, monkeypatch):
    calls = 0

    def unexpected_check(recent_messages):
        """Count calls so the throttle path proves no worker was started."""
        nonlocal calls
        calls += 1

    monkeypatch.setattr(bot, "_check_markets", unexpected_check)
    bot._last_market_check = time.time()
    before = {id(t) for t in threading.enumerate() if t.name == "market-check"}

    bot._maybe_spawn_market_check([])
    time.sleep(0.05)

    after = {id(t) for t in threading.enumerate() if t.name == "market-check"}
    assert calls == 0
    assert after == before


def test_market_check_thread_releases_lock_after_exception(bot, monkeypatch):
    started = threading.Event()
    threads = []

    def crashing_check(recent_messages):
        """Raise from the worker so the finally block must release the lock."""
        threads.append(threading.current_thread())
        started.set()
        raise RuntimeError("boom")

    monkeypatch.setattr(bot, "_check_markets", crashing_check)
    bot._last_market_check = 0

    bot._maybe_spawn_market_check([])
    assert started.wait(timeout=0.5)
    threads[0].join(timeout=1)

    assert bot._market_check_lock.acquire(blocking=False)
    bot._market_check_lock.release()
