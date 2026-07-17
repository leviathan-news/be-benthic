"""Concurrency tests for Benthic bot message processing.

The poll loop should keep fetching updates while slow responses are running.
These tests exercise the internal dispatch seam directly so they can verify
cross-sender parallelism, per-sender serialization, deduplication, and state
locking without starting Telegram polling.
"""

from concurrent.futures import ThreadPoolExecutor
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
    """Import benthic-bot.py under a Python-safe module name for direct tests."""
    os.environ["BENTHIC_BOT_TOKEN"] = "test:stub-token-do-not-use"
    os.environ["WALLET_PRIVATE_KEY"] = ""
    if "benthic_bot_under_test" in sys.modules:
        return sys.modules["benthic_bot_under_test"]
    spec = importlib.util.spec_from_file_location(
        "benthic_bot_under_test", ROOT / "benthic-bot.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["benthic_bot_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


def _message(bot, message_id, sender_id, text=None):
    """Build a Telegram-like group message that directly mentions Benthic."""
    return {
        "message_id": message_id,
        "date": int(time.time()),
        "chat": {
            "id": bot.AGENTS_GROUP_ID,
            "type": "supergroup",
            "title": "Leviathan Agents",
        },
        "from": {
            "id": sender_id,
            "username": f"user{sender_id}",
            "is_bot": False,
        },
        "text": text or f"@Benthic_Bot test message {message_id}",
    }


def _api_message(bot, message_id, sender_id, text):
    """Build the API representation of a Telegram-originated group mention."""
    return {
        "message_id": message_id,
        "date": int(time.time()),
        "chat": {"id": bot.AGENTS_GROUP_ID, "type": "supergroup"},
        "from": {
            "id": sender_id,
            "username": f"api-user{sender_id}",
            "is_bot": True,
        },
        "text": text,
        "message_thread_id": 1,
    }


@pytest.fixture
def bot(monkeypatch):
    """Give each test a fresh processing pool and clean in-memory bot state."""
    mod = _load_bot_module()
    old_pool = mod._PROC_POOL
    old_sender_locks = mod._sender_locks
    old_last_reply = dict(mod._last_reply_to)
    old_responded = set(mod._responded)
    old_api_responded = set(mod._api_responded)
    old_content_responded = dict(mod._content_responded)
    old_thread_depth = dict(mod._thread_depth)
    old_msg_root = dict(mod._msg_root)

    mod._PROC_POOL = ThreadPoolExecutor(max_workers=6, thread_name_prefix="test-msgproc")
    mod._sender_locks = {}
    mod._last_reply_to.clear()
    mod._responded.clear()
    mod._api_responded.clear()
    mod._content_responded.clear()
    mod._thread_depth.clear()
    mod._msg_root.clear()

    sent = []

    def fake_send_message(chat_id, text, **kwargs):
        """Record outbound messages and return a successful Telegram-like result."""
        sent.append((chat_id, text, kwargs))
        return {"ok": True, "result": {"message_id": 900000 + len(sent)}}

    monkeypatch.setattr(mod, "send_message", fake_send_message)
    monkeypatch.setattr(mod, "save_own_action", lambda *args, **kwargs: None)
    monkeypatch.setattr(mod, "save_chat_message", lambda *args, **kwargs: None)
    monkeypatch.setattr(mod, "_try_api_command", lambda *args, **kwargs: None)
    monkeypatch.setattr(mod.time, "sleep", lambda *args, **kwargs: None)
    # Unit workers must not register fake replies through the live authenticated
    # agent-chat relay loaded from the developer machine's wallet file.
    monkeypatch.setattr(mod, "_relay", None)
    mod._test_sent_messages = sent

    yield mod

    mod._PROC_POOL.shutdown(wait=True, cancel_futures=True)
    mod._PROC_POOL = old_pool
    mod._sender_locks = old_sender_locks
    mod._last_reply_to.clear()
    mod._last_reply_to.update(old_last_reply)
    mod._responded.clear()
    mod._responded.update(old_responded)
    mod._api_responded.clear()
    mod._api_responded.update(old_api_responded)
    mod._content_responded.clear()
    mod._content_responded.update(old_content_responded)
    mod._thread_depth.clear()
    mod._thread_depth.update(old_thread_depth)
    mod._msg_root.clear()
    mod._msg_root.update(old_msg_root)


def test_cross_sender_processing_runs_in_parallel(bot, monkeypatch):
    entered = {101: threading.Event(), 202: threading.Event()}
    release = {101: threading.Event(), 202: threading.Event()}

    def blocked_generate(msg, **kwargs):
        """Block each sender inside generation so parallel entry is observable."""
        sender_id = msg["from"]["id"]
        entered[sender_id].set()
        assert release[sender_id].wait(timeout=2)
        return f"reply from {sender_id}"

    monkeypatch.setattr(bot, "generate_response", blocked_generate)

    future_a = bot._dispatch_one_message(_message(bot, 1, 101), {})
    future_b = bot._dispatch_one_message(_message(bot, 2, 202), {})

    assert entered[101].wait(timeout=1)
    assert entered[202].wait(timeout=1)

    release[101].set()
    release[202].set()
    future_a.result(timeout=2)
    future_b.result(timeout=2)


def test_dispatch_treats_natural_name_address_as_direct(bot, monkeypatch):
    """A leading natural Benthic address reaches generation as a direct turn."""
    observed = []

    def capture_directness(msg, **kwargs):
        observed.append(kwargs["is_direct"])
        return False

    monkeypatch.setattr(bot, "generate_response", capture_directness)

    future = bot._dispatch_one_message(
        _message(bot, 3, 203, "Benthic can you review this?"),
        {},
    )
    future.result(timeout=2)

    assert observed == [True]


def test_same_sender_processing_is_serialized(bot, monkeypatch):
    first_entered = threading.Event()
    second_entered = threading.Event()
    release_first = threading.Event()
    release_second = threading.Event()

    def blocked_generate(msg, **kwargs):
        """Hold the first same-sender response until the test releases it."""
        if msg["message_id"] == 10:
            first_entered.set()
            assert release_first.wait(timeout=2)
        else:
            second_entered.set()
            assert release_second.wait(timeout=2)
        return f"reply {msg['message_id']}"

    monkeypatch.setattr(bot, "generate_response", blocked_generate)

    future_first = bot._dispatch_one_message(
        _message(bot, 10, 303, "@Benthic_Bot first"), {})
    assert first_entered.wait(timeout=1)
    future_second = bot._dispatch_one_message(
        _message(bot, 11, 303, "@Benthic_Bot second"), {})

    time.sleep(0.1)
    assert not second_entered.is_set()

    release_first.set()
    assert second_entered.wait(timeout=1)
    release_second.set()
    future_first.result(timeout=2)
    future_second.result(timeout=2)


def test_duplicate_dispatch_replies_once(bot, monkeypatch):
    monkeypatch.setattr(bot, "generate_response", lambda *args, **kwargs: "single reply")
    msg = _message(bot, 20, 404, "@Benthic_Bot duplicate")

    future_one = bot._dispatch_one_message(dict(msg), {})
    future_two = bot._dispatch_one_message(dict(msg), {})

    future_one.result(timeout=2)
    future_two.result(timeout=2)

    assert [text for _, text, _ in bot._test_sent_messages] == ["single reply"]


def test_cross_path_inflight_race_replies_once_with_different_sender_ids(bot, monkeypatch):
    """Telegram and API workers must serialize on normalized message content.

    The API can expose a sender ID that differs from Telegram's ID, so their
    per-sender locks do not close this race. The API worker starts while the
    Telegram worker is still generating and must recheck content dedup only
    after the Telegram response is committed.
    """
    text = "@Benthic_Bot what is the current Ethereum mainnet block?"
    telegram_entered = threading.Event()
    api_entered = threading.Event()
    release_telegram = threading.Event()

    def blocked_generate(msg, **kwargs):
        """Hold Telegram generation while recording any concurrent API entry."""
        if msg["message_id"] == 40:
            telegram_entered.set()
            assert release_telegram.wait(timeout=2)
            return "telegram reply"
        api_entered.set()
        return "api reply"

    monkeypatch.setattr(bot, "generate_response", blocked_generate)

    telegram_future = bot._dispatch_one_message(
        _message(bot, 40, 7001, text), {})
    assert telegram_entered.wait(timeout=1)
    api_future = bot._dispatch_api_mention(
        _api_message(bot, 8001, 9001, text), [])

    api_entered_before_telegram_finished = api_entered.wait(timeout=0.2)
    release_telegram.set()
    telegram_future.result(timeout=2)
    api_future.result(timeout=2)

    assert api_entered_before_telegram_finished is False
    assert api_entered.is_set() is False
    assert [text for _, text, _ in bot._test_sent_messages] == ["telegram reply"]
    assert 8001 in bot._api_responded


def test_grounded_cross_ingress_delivery_still_sends_once(bot, monkeypatch):
    """Both ingress workers share generation and deduplicate before a second send."""
    text = "@Benthic_Bot source-check this claim"
    telegram = _message(bot, 8101, 55, text=text)
    api = _api_message(bot, 9101, 999, text)
    entered = threading.Event()
    release = threading.Event()

    def generate(*args, **kwargs):
        """Hold the first common generation call while API delivery queues."""
        entered.set()
        assert release.wait(timeout=2)
        return "Verified once."

    monkeypatch.setattr(bot, "generate_response", generate)
    monkeypatch.setattr(
        bot,
        "_finalize_generated_response",
        lambda response, *args, **kwargs: response,
    )
    first = bot._PROC_POOL.submit(
        bot._process_one_message, telegram, [], True, False
    )
    assert entered.wait(timeout=2)
    second = bot._PROC_POOL.submit(bot._process_api_mention, api, [])
    release.set()
    first.result(timeout=3)
    second.result(timeout=3)

    assert [row[1] for row in bot._test_sent_messages] == ["Verified once."]


def test_state_lock_is_not_held_during_generation(bot, monkeypatch):
    checked = threading.Event()

    def lock_checking_generate(msg, **kwargs):
        """Generation must be able to acquire the state lock non-blockingly."""
        acquired = bot._state_lock.acquire(blocking=False)
        try:
            assert acquired
            checked.set()
            return "lock invariant reply"
        finally:
            if acquired:
                bot._state_lock.release()

    monkeypatch.setattr(bot, "generate_response", lock_checking_generate)

    future = bot._dispatch_one_message(_message(bot, 30, 505), {})
    future.result(timeout=2)

    assert checked.is_set()
    assert [text for _, text, _ in bot._test_sent_messages] == ["lock invariant reply"]


def test_prune_state_is_safe_while_worker_adds(bot, monkeypatch):
    now = time.time()
    bot._api_responded.update(range(bot._MAX_STATE_SIZE + 100))
    bot._responded.update(range(bot._MAX_STATE_SIZE + 100))
    bot._content_responded.update({
        i: now for i in range(bot._MAX_STATE_SIZE + 100)
    })
    bot._last_reply_to.update({
        i: now - 4000 for i in range(bot._MAX_STATE_SIZE + 100)
    })

    entered_generate = threading.Event()
    release_generate = threading.Event()
    send_called = threading.Event()

    def blocked_generate(msg, **kwargs):
        """Pause before send so the test can hold the state lock during add."""
        entered_generate.set()
        assert release_generate.wait(timeout=2)
        return "prune reply"

    def send_and_signal(chat_id, text, **kwargs):
        """Signal that the worker is about to reach its state-marking section."""
        send_called.set()
        return {"ok": True, "result": {"message_id": 999999}}

    monkeypatch.setattr(bot, "generate_response", blocked_generate)
    monkeypatch.setattr(bot, "send_message", send_and_signal)

    future = bot._dispatch_one_message(_message(bot, 9000, 606), {})
    assert entered_generate.wait(timeout=1)

    with bot._state_lock:
        release_generate.set()
        assert send_called.wait(timeout=1)
        if len(bot._api_responded) > bot._MAX_STATE_SIZE:
            bot._prune_set(bot._api_responded)
        if len(bot._content_responded) > bot._MAX_STATE_SIZE:
            bot._prune_content_dedup()
        if len(bot._responded) > bot._MAX_STATE_SIZE:
            bot._prune_set(bot._responded)
        stale = [k for k, v in bot._last_reply_to.items() if time.time() - v > 3600]
        for k in stale:
            del bot._last_reply_to[k]

    future.result(timeout=2)

    assert len(bot._api_responded) <= bot._MAX_STATE_SIZE // 2
    assert len(bot._responded) <= bot._MAX_STATE_SIZE // 2 + 1
    assert len(bot._content_responded) <= bot._MAX_STATE_SIZE // 2 + 2
    assert list(bot._last_reply_to) == [606]
