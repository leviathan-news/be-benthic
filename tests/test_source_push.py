"""Tests for real-time source ingestion (pending queue, push handler, news mini-pass, run_loop scheduler)."""

import asyncio
import importlib.util
import inspect
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


def _load_agent_module():
    if "ln_agent_push_test" in sys.modules:
        return sys.modules["ln_agent_push_test"]
    spec = importlib.util.spec_from_file_location(
        "ln_agent_push_test", ROOT / "ln-agent.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ln_agent_push_test"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def agent(tmp_path):
    mod = _load_agent_module()
    db = mod.AgentDB(db_path=tmp_path / "test.db")
    yield mod, db
    db.close()


# ── pending_source_messages queue ────────────────────────────────────────

def test_add_pending_inserts_and_dedups(agent):
    mod, db = agent
    assert db.add_pending_source_message("@c", 10, "text one",
                                         "2026-08-02T10:00:00+00:00") is True
    assert db.add_pending_source_message("@c", 10, "text one",
                                         "2026-08-02T10:00:00+00:00") is False
    assert db.add_pending_source_message("@c", 11, "text two",
                                         "2026-08-02T10:01:00+00:00",
                                         url="https://a.com/x") is True


def test_get_and_consume_pending(agent):
    mod, db = agent
    for i in range(5):
        db.add_pending_source_message("@c", i, f"t{i}", "2026-08-02T10:00:00+00:00")
    rows = db.get_pending_source_messages(limit=3)
    assert [r["msg_id"] for r in rows] == [0, 1, 2]  # oldest first, capped
    db.mark_pending_source_consumed([r["id"] for r in rows])
    rows2 = db.get_pending_source_messages(limit=10)
    assert [r["msg_id"] for r in rows2] == [3, 4]


def test_prune_pending(agent):
    mod, db = agent
    db.add_pending_source_message("@c", 1, "old", "2026-07-01T00:00:00+00:00")
    old = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    db._execute("UPDATE pending_source_messages SET received_at = ?", (old,))
    db._commit()
    db.add_pending_source_message("@c", 2, "new", "2026-08-02T10:00:00+00:00")
    assert db.prune_pending_source_messages(days=7) == 1
    assert [r["msg_id"] for r in db.get_pending_source_messages(limit=10)] == [2]


# ── Push handler ─────────────────────────────────────────────────────────

def _fake_event(mod, chat_id, msg_id, text):
    return SimpleNamespace(
        chat_id=chat_id,
        message=SimpleNamespace(
            id=msg_id, text=text,
            date=datetime(2026, 8, 2, 10, 0, tzinfo=timezone.utc)))


def test_handler_queues_prefiltered_and_wakes(agent):
    mod, db = agent
    marked = -1001000000111
    handler = mod.make_source_handler(db, {marked: ("@src", False)})
    mod._news_wake.clear()
    asyncio.run(handler(_fake_event(mod, marked, 5, "Breaking: https://a.com/story")))
    rows = db.get_pending_source_messages(limit=10)
    assert [(r["channel"], r["msg_id"]) for r in rows] == [("@src", 5)]
    assert rows[0]["url"] == "https://a.com/story"
    assert mod._news_wake.is_set()


def test_handler_drops_noise_and_unknown_chats(agent):
    mod, db = agent
    marked = -1001000000111
    handler = mod.make_source_handler(db, {marked: ("@src", False)})
    mod._news_wake.clear()
    asyncio.run(handler(_fake_event(mod, marked, 6, "gm")))                    # pre-filter drop
    asyncio.run(handler(_fake_event(mod, -100999, 7, "Breaking: https://x.co/a")))  # unknown chat
    assert db.get_pending_source_messages(limit=10) == []
    assert not mod._news_wake.is_set()


def test_handler_dedups_and_survives_errors(agent):
    mod, db = agent
    marked = -1001000000111
    handler = mod.make_source_handler(db, {marked: ("@src", False)})
    ev = _fake_event(mod, marked, 8, "Breaking: https://a.com/s")
    asyncio.run(handler(ev))
    asyncio.run(handler(ev))  # duplicate → INSERT OR IGNORE
    assert len(db.get_pending_source_messages(limit=10)) == 1
    # A broken event object must not raise out of the handler
    asyncio.run(handler(SimpleNamespace(chat_id=marked, message=None)))


def test_handler_validates_url_and_stores_text_raw(agent):
    """URL hygiene pin: the extracted URL goes through validate_url() before storage.

    A rejected URL (here: oversized) must not poison the queue row — the message
    itself still queues so the mini-pass can evaluate it, just without a URL.
    The text is stored RAW; sanitization happens at prompt-build time.
    """
    mod, db = agent
    marked = -1001000000111
    handler = mod.make_source_handler(db, {marked: ("@src", False)})
    hostile = "Breaking: https://a.com/" + "x" * 2100
    asyncio.run(handler(_fake_event(mod, marked, 9, hostile)))
    rows = db.get_pending_source_messages(limit=10)
    assert len(rows) == 1
    assert rows[0]["url"] is None           # validate_url rejected it
    assert rows[0]["text"] == hostile       # text kept raw

    # Trailing sentence punctuation is stripped (same cleanup as the hourly path)
    asyncio.run(handler(_fake_event(mod, marked, 10, "Breaking: https://a.com/s.")))
    row = [r for r in db.get_pending_source_messages(limit=10) if r["msg_id"] == 10][0]
    assert row["url"] == "https://a.com/s"


def test_caption_edit_queues_previously_ineligible_notification_once(agent):
    from telethon.tl.types import Message, MessageEntityTextUrl, PeerChannel
    from telethon.extensions import markdown
    mod, db = agent
    marked = -1001000000111
    mapping = {marked: ("@src", False)}
    new_handler = mod.make_source_handler(db, mapping)
    edit_handler = mod.make_source_handler(db, mapping, edited=True)
    original = _fake_event(mod, marked, 21, "")
    original.message.date = datetime.now(timezone.utc) - timedelta(seconds=4)
    asyncio.run(new_handler(original))
    assert not db.get_pending_source_messages()
    message = Message(id=21, peer_id=PeerChannel(1000000111),
                      date=original.message.date, message="Security warning",
                      entities=[MessageEntityTextUrl(0, 16, "https://x.com/official/status/123")])
    message._client = SimpleNamespace(parse_mode=markdown)
    edited = SimpleNamespace(chat_id=marked, message=message)
    mod._news_wake.clear()
    asyncio.run(edit_handler(edited))
    assert mod._news_wake.is_set()
    rows = db.get_pending_source_messages()
    assert len(rows) == 1 and rows[0]["msg_id"] == 21
    assert rows[0]["url"] == "https://x.com/official/status/123"
    assert rows[0]["msg_date"] == original.message.date.isoformat()
    mod._news_wake.clear()
    asyncio.run(edit_handler(edited))
    assert len(db.get_pending_source_messages()) == 1
    assert not mod._news_wake.is_set()


@pytest.mark.parametrize("age,known", [(timedelta(days=4), True),
                                       (timedelta(minutes=-10), True),
                                       (timedelta(seconds=4), False)])
def test_source_edits_preserve_scope_and_original_message_age(agent, age, known):
    mod, db = agent
    marked = -1001000000111
    handler = mod.make_source_handler(db, {marked: ("@src", False)}, edited=True)
    event = _fake_event(mod, marked if known else -100999, 22, "https://example.com/news")
    event.message.date = datetime.now(timezone.utc) - age
    asyncio.run(handler(event))
    assert not db.get_pending_source_messages()


def test_register_source_handlers_builds_map(agent, monkeypatch):
    mod, db = agent
    db.save_channel_id("@one", 1000000111, "One", "channel")
    db.save_channel_id("@two", 1000000222, "Two", "group")
    monkeypatch.setattr(mod, "CHANNELS", ["@one", "@two", "@broken"])

    async def fake_resolve(client, channel, _db):
        if channel == "@broken":
            raise RuntimeError("cannot resolve")
        return _db.get_channel_id(channel)
    monkeypatch.setattr(mod, "resolve_channel", fake_resolve)

    handlers = []

    class FakeClient:
        def add_event_handler(self, cb, event_filter):
            handlers.append({"cb": cb, "filter": event_filter})

    n = asyncio.run(mod.register_source_handlers(FakeClient(), db))
    assert n == 2 and len(handlers) == 2
    registered = handlers[0]

    # New messages and caption edits cover the same configured source IDs.
    from telethon import events, utils
    from telethon.tl import types
    marked_one = utils.get_peer_id(types.PeerChannel(1000000111))
    marked_two = utils.get_peer_id(types.PeerChannel(1000000222))
    assert isinstance(registered["filter"], events.NewMessage)
    assert isinstance(handlers[1]["filter"], events.MessageEdited)
    assert sorted(registered["filter"].chats) == sorted([marked_one, marked_two])
    assert sorted(handlers[1]["filter"].chats) == sorted([marked_one, marked_two])

    # The registered callback routes by MARKED id and honors is_group
    mod._news_wake.clear()
    # text-only breaking news in a GROUP is dropped (no URL + is_group)
    asyncio.run(registered["cb"](_fake_event(mod, marked_two, 3, "breaking: exploit confirmed")))
    assert db.get_pending_source_messages(limit=10) == []

    # Canonical-naming pin: the queue row stores the @username the hourly cycle's
    # channel_cursors/evaluated_messages join against — never a numeric peer id.
    asyncio.run(registered["cb"](_fake_event(mod, marked_two, 4, "Breaking: https://a.com/g")))
    asyncio.run(registered["cb"](_fake_event(mod, marked_one, 4, "Breaking: https://a.com/c")))
    rows = db.get_pending_source_messages(limit=10)
    assert sorted(r["channel"] for r in rows) == ["@one", "@two"]


# ── News mini-pass ───────────────────────────────────────────────────────

@pytest.fixture
def news_pass_env(agent, monkeypatch):
    mod, db = agent
    # run_news_pass opens its own AgentDB() — point it at the test DB file.
    monkeypatch.setattr(mod, "AgentDB", lambda: type(db)(db_path=db.db_path))
    calls = {"eval": [], "post": [], "creds": 0}

    def fake_eval(messages, _db):
        calls["eval"].append(list(messages))
        return [dict(m, url="https://a.com/x", headline_hint="h", reason="r", heat=5)
                for m in messages]
    monkeypatch.setattr(mod, "evaluate_and_deduplicate", fake_eval)

    async def fake_post(relevant, _db, _ln):
        calls["post"].append(list(relevant))
        return len(relevant)
    monkeypatch.setattr(mod, "news_post_pass", fake_post)

    def fake_creds():
        calls["creds"] += 1
        return ("id", "hash", "walletkey")
    monkeypatch.setattr(mod, "load_credentials", fake_creds)

    class FakeLN:
        def __init__(self, key):
            pass

        def authenticate(self):
            pass
    monkeypatch.setattr(mod, "LNClient", FakeLN)
    return mod, db, calls


def test_news_pass_consumes_before_eval_and_caps(news_pass_env, monkeypatch):
    mod, db, calls = news_pass_env
    monkeypatch.setattr(mod, "NEWS_PASS_MAX_MESSAGES", 2)
    order = []
    real_mark = db.__class__.mark_pending_source_consumed

    def spy_mark(self, ids):
        order.append("consume")
        return real_mark(self, ids)
    monkeypatch.setattr(db.__class__, "mark_pending_source_consumed", spy_mark)

    def eval_spy(messages, _db):
        order.append("eval")
        return []
    monkeypatch.setattr(mod, "evaluate_and_deduplicate", eval_spy)

    for i in range(4):
        db.add_pending_source_message("@c", i, f"Breaking: https://a.com/{i}",
                                      "2026-08-02T10:00:00+00:00")
    asyncio.run(mod.run_news_pass())
    assert order == ["consume", "eval"]          # consume-on-drain BEFORE LLM
    assert len(db.get_pending_source_messages(limit=10)) == 2  # cap left the rest


def test_news_pass_skips_already_evaluated(news_pass_env):
    mod, db, calls = news_pass_env
    db.add_pending_source_message("@c", 1, "Breaking: https://a.com/1",
                                  "2026-08-02T10:00:00+00:00")
    db.add_pending_source_message("@c", 2, "Breaking: https://a.com/2",
                                  "2026-08-02T10:00:00+00:00")
    db.save_evaluation("@c", 1, "already seen", is_newsworthy=False, reason="x")
    asyncio.run(mod.run_news_pass())
    assert len(calls["eval"]) == 1
    assert [m["id"] for m in calls["eval"][0]] == [2]
    # message dicts carry the cycle-path shape
    assert set(calls["eval"][0][0]) >= {"channel", "id", "text", "date", "is_group"}


def test_news_pass_empty_queue_no_llm_no_auth(news_pass_env):
    mod, db, calls = news_pass_env
    asyncio.run(mod.run_news_pass())
    assert calls["eval"] == [] and calls["post"] == [] and calls["creds"] == 0


def test_news_pass_no_newsworthy_skips_ln_auth(news_pass_env, monkeypatch):
    mod, db, calls = news_pass_env
    monkeypatch.setattr(mod, "evaluate_and_deduplicate", lambda m, d: [])
    db.add_pending_source_message("@c", 3, "Breaking: https://a.com/3",
                                  "2026-08-02T10:00:00+00:00")
    asyncio.run(mod.run_news_pass())
    assert calls["creds"] == 0 and calls["post"] == []


def test_guarded_news_pass_sets_anchor_and_contains_errors(news_pass_env, monkeypatch):
    mod, db, calls = news_pass_env

    async def boom():
        raise RuntimeError("pass exploded")
    monkeypatch.setattr(mod, "run_news_pass", boom)
    before = mod._last_news_pass_ts
    asyncio.run(mod._run_guarded_news_pass())   # must not raise
    assert mod._last_news_pass_ts > before


def test_guarded_news_pass_deadline(news_pass_env, monkeypatch):
    mod, db, calls = news_pass_env
    monkeypatch.setattr(mod, "NEWS_PASS_DEADLINE", 0.05)

    async def slow():
        await asyncio.sleep(1.0)
    monkeypatch.setattr(mod, "run_news_pass", slow)
    t0 = time.monotonic()
    asyncio.run(mod._run_guarded_news_pass())   # aborts at deadline, no raise
    assert time.monotonic() - t0 < 0.8


def test_deadline_abort_leaves_rows_consumed(news_pass_env, monkeypatch):
    """2026-07-02 production lesson: an aborted pass must NOT re-drain its rows.

    The deadline fires inside the posting stage (after the drain); the rows stay
    consumed, so the next pass starts on fresh input and the hourly cycle is the
    only backfill path for whatever the abort skipped.
    """
    mod, db, calls = news_pass_env
    monkeypatch.setattr(mod, "NEWS_PASS_DEADLINE", 0.05)

    async def slow_post(relevant, _db, _ln):
        await asyncio.sleep(1.0)
        return 0
    monkeypatch.setattr(mod, "news_post_pass", slow_post)

    db.add_pending_source_message("@c", 20, "Breaking: https://a.com/20",
                                  "2026-08-02T10:00:00+00:00")
    t0 = time.monotonic()
    asyncio.run(mod._run_guarded_news_pass())
    assert time.monotonic() - t0 < 0.8
    assert db.get_pending_source_messages(limit=10) == []   # still consumed


def test_news_and_vote_anchors_are_independent(news_pass_env, monkeypatch):
    """Rename pin: one shared anchor would let a busy vote mini-pass starve news
    ingestion (and vice versa). Each guarded pass stamps only its own."""
    mod, db, calls = news_pass_env
    monkeypatch.setattr(mod, "_last_vote_pass_ts", 0.0)
    monkeypatch.setattr(mod, "_last_news_pass_ts", 0.0)

    async def noop():
        return None
    monkeypatch.setattr(mod, "run_news_pass", noop)
    monkeypatch.setattr(mod, "run_mini_pass", noop)

    asyncio.run(mod._run_guarded_news_pass())
    assert mod._last_news_pass_ts > 0.0 and mod._last_vote_pass_ts == 0.0

    monkeypatch.setattr(mod, "_last_news_pass_ts", 0.0)
    asyncio.run(mod._run_guarded_mini_pass())
    assert mod._last_vote_pass_ts > 0.0 and mod._last_news_pass_ts == 0.0


def test_news_pass_evaluates_off_the_event_loop(news_pass_env, monkeypatch):
    """Co-tenancy pin: the provider call is blocking and can run for ~900s.

    On the shared loop it would freeze Telethon push dispatch and the WS
    listener, and asyncio.wait_for could not enforce NEWS_PASS_DEADLINE.
    """
    mod, db, calls = news_pass_env
    seen = {}

    def thread_recording_eval(messages, _db):
        seen["thread"] = threading.get_ident()
        return []
    monkeypatch.setattr(mod, "evaluate_and_deduplicate", thread_recording_eval)

    db.add_pending_source_message("@c", 30, "Breaking: https://a.com/30",
                                  "2026-08-02T10:00:00+00:00")
    asyncio.run(mod.run_news_pass())
    assert seen["thread"] != threading.get_ident()


# ── Cold-cache registration pacing ───────────────────────────────────────

def test_register_source_handlers_paces_only_cache_misses(agent, monkeypatch):
    """Flood-wait pin: a COLD channel_ids cache turns registration into one
    ResolveUsernameRequest per channel (~68 back-to-back). Pace the misses;
    a warm cache — the normal case and EVERY client rebuild — stays instant."""
    mod, db = agent
    db.save_channel_id("@warm", 1000000111, "Warm", "channel")
    monkeypatch.setattr(mod, "CHANNELS", ["@warm", "@cold1", "@cold2"])
    monkeypatch.setattr(mod, "PUSH_RESOLVE_PACING", 0.25)

    next_id = [1000000200]

    async def fake_resolve(client, channel, _db):
        cached = _db.get_channel_id(channel)
        if cached:
            return cached
        next_id[0] += 1
        _db.save_channel_id(channel, next_id[0], channel, "channel")
        return next_id[0]
    monkeypatch.setattr(mod, "resolve_channel", fake_resolve)

    slept = []

    async def fake_sleep(delay, *a, **kw):
        slept.append(delay)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    class FakeClient:
        def add_event_handler(self, cb, event_filter):
            pass

    assert asyncio.run(mod.register_source_handlers(FakeClient(), db)) == 3
    assert slept == [0.25, 0.25]   # the two cache misses only


def test_register_source_handlers_paces_failed_cold_resolution(agent, monkeypatch):
    """A cold channel that FAILS to resolve still consumed an API call — pace it
    too, or a run of bad usernames re-creates the unpaced burst."""
    mod, db = agent
    monkeypatch.setattr(mod, "CHANNELS", ["@bad"])
    monkeypatch.setattr(mod, "PUSH_RESOLVE_PACING", 0.25)

    async def fake_resolve(client, channel, _db):
        raise RuntimeError("cannot resolve")
    monkeypatch.setattr(mod, "resolve_channel", fake_resolve)

    slept = []

    async def fake_sleep(delay, *a, **kw):
        slept.append(delay)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    class FakeClient:
        def add_event_handler(self, cb, event_filter):
            raise AssertionError("nothing resolvable — must not register")

    assert asyncio.run(mod.register_source_handlers(FakeClient(), db)) == 0
    assert slept == [0.25]


# ── run_agent signature + hourly filter wiring ───────────────────────────

def test_run_agent_uses_the_shared_client(agent):
    """The client is process-lifetime now: run_agent must neither construct nor
    disconnect it — either would kill the registered push handlers every cycle."""
    mod, _ = agent
    assert list(inspect.signature(mod.run_agent).parameters) == ["client"]
    src = inspect.getsource(mod.run_agent)
    assert "TelegramClient(" not in src
    assert "client.disconnect()" not in src
    # The filter feeds evaluation ONLY. The early return stays keyed on the raw
    # read, so a fully-push-evaluated hour still reaches Phases 4-6.
    assert "fresh_messages = _drop_already_evaluated(all_messages, db)" in src
    assert "_blocking(evaluate_and_deduplicate, fresh_messages, db)" in src
    assert src.index("if not all_messages:") < src.index("fresh_messages =")


# ── Full cycle: the hourly filter must not skip the engagement phases ─────

@pytest.fixture
def cycle_env(agent, monkeypatch):
    """Minimal harness for one real run_agent() pass over a fake client."""
    mod, db = agent
    monkeypatch.setattr(mod, "AgentDB", lambda: type(db)(db_path=db.db_path))
    monkeypatch.setattr(mod, "CHANNELS", ["@c"])
    monkeypatch.setattr(mod, "ID_SOURCES", {})
    monkeypatch.setattr(mod, "ENABLE_MARKET_MATCH", False)
    monkeypatch.setattr(mod, "load_credentials", lambda: ("id", "hash", "key"))
    calls = {"eval": [], "post": 0, "vote": 0}
    inbox = [{"channel": "@c", "id": 1, "text": "Breaking: https://a.com/1",
              "date": "2026-08-02T10:00:00+00:00", "is_group": False}]

    async def fake_resolve(client, channel, _db):
        return 111
    monkeypatch.setattr(mod, "resolve_channel", fake_resolve)

    async def fake_fetch(client, channel, min_id=0, limit=50, since=None,
                         channel_name=None, is_group=False):
        return [dict(m) for m in inbox]
    monkeypatch.setattr(mod, "fetch_channel_messages", fake_fetch)

    def fake_eval(messages, _db):
        calls["eval"].append([m["id"] for m in messages])
        return []
    monkeypatch.setattr(mod, "evaluate_and_deduplicate", fake_eval)

    async def fake_post(relevant, _db, _ln):
        calls["post"] += 1
        return 0
    monkeypatch.setattr(mod, "news_post_pass", fake_post)

    async def fake_vote(ln, _db, articles, since):
        calls["vote"] += 1
        return (0, 0, set())
    monkeypatch.setattr(mod, "vote_comment_pass", fake_vote)

    class FakeLN:
        def __init__(self, key):
            self._lock = threading.Lock()
            self._auth_time = 0

        def authenticate(self):
            pass

        def _refresh_if_stale(self):
            pass

        def get_recent_articles(self, per_page=20):
            return []
    monkeypatch.setattr(mod, "LNClient", FakeLN)

    class FakeClient:
        def iter_dialogs(self):
            async def _empty():
                return
                yield  # pragma: no cover - makes this an async generator
            return _empty()

    return mod, db, calls, inbox, FakeClient()


def test_cycle_still_runs_engagement_when_all_messages_were_push_evaluated(cycle_env):
    """Regression pin: the filter must not short-circuit the cycle.

    A quiet hour whose only messages a news mini-pass already evaluated still
    owes Phase 4/5/6 work (voting, reply detection, market match). Filtering
    before the `if not all_messages` early return silently skipped all three.
    """
    mod, db, calls, inbox, client = cycle_env
    db.save_evaluation("@c", 1, "Breaking: https://a.com/1",
                       is_newsworthy=False, reason="push pass")
    asyncio.run(mod.run_agent(client))
    assert calls["eval"] == [[]]   # nothing left to re-bill to the provider
    assert calls["vote"] == 1      # ...but Phase 4 STILL ran


def test_cycle_exits_early_only_when_the_read_was_empty(cycle_env):
    mod, db, calls, inbox, client = cycle_env
    inbox.clear()
    asyncio.run(mod.run_agent(client))
    assert calls["eval"] == [] and calls["vote"] == 0


def test_cycle_evaluates_only_the_unevaluated_messages(cycle_env):
    mod, db, calls, inbox, client = cycle_env
    inbox.append({"channel": "@c", "id": 2, "text": "Breaking: https://a.com/2",
                  "date": "2026-08-02T10:01:00+00:00", "is_group": False})
    db.save_evaluation("@c", 1, "Breaking: https://a.com/1",
                       is_newsworthy=False, reason="push pass")
    asyncio.run(mod.run_agent(client))
    assert calls["eval"] == [[2]]
    assert calls["vote"] == 1


def test_hourly_read_filters_already_evaluated(agent):
    """The filter helper drops (channel,msg_id) pairs the push path evaluated."""
    mod, db = agent
    db.save_evaluation("@c", 1, "seen", is_newsworthy=False, reason="x")
    msgs = [{"channel": "@c", "id": 1, "text": "seen"},
            {"channel": "@c", "id": 2, "text": "new"}]
    fresh = mod._drop_already_evaluated(msgs, db)
    assert [m["id"] for m in fresh] == [2]


def test_hourly_filter_is_a_noop_without_overlap(agent):
    mod, db = agent
    msgs = [{"channel": "@c", "id": 7, "text": "a"}]
    assert mod._drop_already_evaluated(msgs, db) == msgs
    assert mod._drop_already_evaluated([], db) == []


# ── Client construction ──────────────────────────────────────────────────

def test_build_client_is_deadline_bounded(agent, monkeypatch):
    mod, db = agent
    monkeypatch.setattr(mod, "load_credentials", lambda: (7, "hash", "walletkey"))
    monkeypatch.setattr(mod, "CONNECT_TIMEOUT", 0.05)
    built = {}

    class HangingClient:
        def __init__(self, session, api_id, api_hash):
            built["args"] = (session, api_id, api_hash)

        async def start(self):
            await asyncio.sleep(5)

        async def disconnect(self):
            built["disconnects"] = built.get("disconnects", 0) + 1
    monkeypatch.setattr(mod, "TelegramClient", HangingClient)

    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(asyncio.wait_for(mod._build_client(), timeout=5))
    assert built["args"][1:] == (7, "hash")
    assert built["disconnects"] == 1


def test_build_client_returns_started_client(agent, monkeypatch):
    mod, db = agent
    monkeypatch.setattr(mod, "load_credentials", lambda: (7, "hash", "walletkey"))
    started = []

    class OkClient:
        def __init__(self, session, api_id, api_hash):
            pass

        async def start(self):
            started.append(True)
    monkeypatch.setattr(mod, "TelegramClient", OkClient)

    client = asyncio.run(mod._build_client())
    assert isinstance(client, OkClient) and started == [True]
    assert not hasattr(client, "disconnected")  # healthy start is left alone


@pytest.mark.parametrize("mode", ["raises", "hangs"])
def test_build_client_disconnects_a_half_started_client(agent, monkeypatch, mode):
    """A failed or timed-out start() still leaves a half-open transport and
    Telethon's background tasks behind — drop it before propagating."""
    mod, db = agent
    monkeypatch.setattr(mod, "load_credentials", lambda: (7, "hash", "walletkey"))
    monkeypatch.setattr(mod, "CONNECT_TIMEOUT", 0.05)
    seen = {"disconnects": 0}

    class BadClient:
        def __init__(self, session, api_id, api_hash):
            pass

        async def start(self):
            if mode == "raises":
                raise RuntimeError("auth key invalid")
            await asyncio.sleep(5)

        async def disconnect(self):
            seen["disconnects"] += 1
    monkeypatch.setattr(mod, "TelegramClient", BadClient)

    expected = RuntimeError if mode == "raises" else asyncio.TimeoutError
    with pytest.raises(expected):
        asyncio.run(asyncio.wait_for(mod._build_client(), timeout=5))
    assert seen["disconnects"] == 1


# ── Bounded push registration ────────────────────────────────────────────

def test_connect_and_register_bounds_registration(agent, monkeypatch):
    """Registration runs outside every watchdog and Telethon sleeps off flood
    waits — so it is bounded. A timeout degrades to poll-only, it never kills
    the client or the process, and no partial handler is left attached."""
    mod, db = agent
    monkeypatch.setattr(mod, "ENABLE_SOURCE_PUSH", True)
    monkeypatch.setattr(mod, "PUSH_REGISTER_TIMEOUT", 0.05)
    added = []

    class FakeClient:
        def __init__(self):
            self._disconnected = None

        def is_connected(self):
            return True

        @property
        def disconnected(self):
            if self._disconnected is None:
                self._disconnected = asyncio.get_running_loop().create_future()
            return asyncio.shield(self._disconnected)

        def add_event_handler(self, cb, event_filter):
            added.append(cb)

        async def disconnect(self):
            if self._disconnected is not None and not self._disconnected.done():
                self._disconnected.set_result(None)

    built = FakeClient()

    async def fake_build():
        return built
    monkeypatch.setattr(mod, "_build_client", fake_build)

    async def flood_waiting_register(client, _db):
        await asyncio.sleep(5)            # Telethon sleeping off a flood wait
        client.add_event_handler(object(), object())
        return 68
    monkeypatch.setattr(mod, "register_source_handlers", flood_waiting_register)

    t0 = time.monotonic()
    client, disconnect_watch = asyncio.run(mod._connect_and_register(db))
    assert time.monotonic() - t0 < 1.0
    assert client is built     # poll continues on the SAME client
    assert added == []         # cancelled before add_event_handler — never partial
    assert disconnect_watch is not None


def test_connect_and_register_returns_none_on_connect_failure(agent, monkeypatch):
    mod, db = agent
    monkeypatch.setattr(mod, "ENABLE_SOURCE_PUSH", True)

    async def failing_build():
        raise RuntimeError("connect refused")
    monkeypatch.setattr(mod, "_build_client", failing_build)
    assert asyncio.run(mod._connect_and_register(db)) == (None, None)


def test_connect_and_register_cancellation_closes_started_client(agent, monkeypatch):
    """Cancellation can land after start() succeeds but while push registration
    is pending. Ownership has not reached run_loop yet, so this function must
    close the client before re-raising cancellation."""
    mod, db = agent
    monkeypatch.setattr(mod, "ENABLE_SOURCE_PUSH", True)
    entered_registration = asyncio.Event()
    disconnects = 0

    class StartedClient:
        async def disconnect(self):
            nonlocal disconnects
            disconnects += 1

    async def fake_build():
        return StartedClient()

    async def pending_registration(client, _db):
        entered_registration.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(mod, "_build_client", fake_build)
    monkeypatch.setattr(mod, "register_source_handlers", pending_registration)

    async def scenario():
        task = asyncio.create_task(mod._connect_and_register(db))
        await entered_registration.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert disconnects == 1


def test_registration_failure_waits_for_terminal_sender_cleanup(agent, monkeypatch):
    """A rejected registration request can observe is_connected=False before
    Telethon finishes terminal sender cleanup. The registration-owned watcher
    must order that cleanup before client.disconnect()."""
    mod, db = agent
    monkeypatch.setattr(mod, "ENABLE_SOURCE_PUSH", True)
    close_order = []

    class TerminalDuringRegistrationClient:
        def __init__(self):
            self.connected = True
            self._disconnected = asyncio.get_running_loop().create_future()

        def is_connected(self):
            return self.connected

        @property
        def disconnected(self):
            return asyncio.shield(self._disconnected)

        async def disconnect(self):
            close_order.append(self._disconnected.done())
            if not self._disconnected.done():
                self._disconnected.set_result(None)

    async def fake_build():
        return TerminalDuringRegistrationClient()

    async def terminal_registration_failure(client, _db):
        client.connected = False

        def finish_terminal_cleanup():
            if not client._disconnected.done():
                client._disconnected.set_exception(
                    ConnectionError("Cannot send requests while disconnected")
                )

        asyncio.get_running_loop().call_later(0.05, finish_terminal_cleanup)
        raise ConnectionError("registration request disconnected")

    monkeypatch.setattr(mod, "_build_client", fake_build)
    monkeypatch.setattr(
        mod, "register_source_handlers", terminal_registration_failure)

    result = asyncio.run(mod._connect_and_register(db))
    assert result is None or result == (None, None)
    assert close_order == [True]


def test_terminal_handoff_shutdown_failure_is_not_reclosed(agent, monkeypatch):
    """Central terminal validation owns cleanup exactly once. A fatal close
    error must propagate without the surrounding exception handler retrying it."""
    mod, db = agent
    monkeypatch.setattr(mod, "ENABLE_SOURCE_PUSH", False)
    shutdown_attempts = 0

    class TerminalClient:
        def __init__(self):
            self._disconnected = asyncio.get_running_loop().create_future()
            self._disconnected.set_exception(ConnectionError("terminal"))

        def is_connected(self):
            return False

        @property
        def disconnected(self):
            return asyncio.shield(self._disconnected)

        async def disconnect(self):
            nonlocal shutdown_attempts
            shutdown_attempts += 1
            raise OSError("session flush failed")

    async def fake_build():
        return TerminalClient()

    monkeypatch.setattr(mod, "_build_client", fake_build)

    with pytest.raises(mod.ClientShutdownError, match="refusing overlapping"):
        asyncio.run(mod._connect_and_register(db))
    assert shutdown_attempts == 1


def test_close_client_fails_stop_on_shielded_cleanup_timeout(agent, monkeypatch):
    """Telethon shields its real cleanup task. If our deadline expires, the
    inner cleanup is still live, so in-process replacement would race the same
    session file and handler teardown."""
    mod, _db = agent
    monkeypatch.setattr(mod, "CLIENT_SHUTDOWN_TIMEOUT", 0.02)
    release = asyncio.Event()
    cleanup_tasks = []

    class ShieldedClient:
        def disconnect(self):
            async def cleanup():
                await release.wait()

            task = asyncio.create_task(cleanup())
            cleanup_tasks.append(task)
            return asyncio.shield(task)

    async def scenario():
        error_type = getattr(mod, "ClientShutdownError", RuntimeError)
        try:
            with pytest.raises(error_type, match="refusing overlapping"):
                await mod._close_client(ShieldedClient())
            assert len(cleanup_tasks) == 1
            assert not cleanup_tasks[0].done()
        finally:
            release.set()
            await asyncio.gather(*cleanup_tasks, return_exceptions=True)

    asyncio.run(scenario())


def test_close_client_fails_stop_on_disconnect_error(agent):
    mod, _db = agent

    class BrokenClient:
        async def disconnect(self):
            raise OSError("session flush failed")

    error_type = getattr(mod, "ClientShutdownError", RuntimeError)
    with pytest.raises(error_type, match="refusing overlapping"):
        asyncio.run(mod._close_client(BrokenClient()))


# ── run_loop scheduler + client lifecycle ────────────────────────────────

@pytest.fixture
def loop_env(agent, monkeypatch):
    mod, db = agent
    monkeypatch.setattr(mod, "AgentDB", lambda: type(db)(db_path=db.db_path))
    monkeypatch.setattr(mod, "ENABLE_WS_EVENTS", False)
    monkeypatch.setattr(mod, "ENABLE_WS_MINI_PASS", False)
    monkeypatch.setattr(mod, "ENABLE_SOURCE_PUSH", True)
    monkeypatch.setattr(mod, "CYCLE_INTERVAL", 30)
    monkeypatch.setattr(mod, "NEWS_PASS_MIN_INTERVAL", 0)
    # asyncio.Event pins itself to the first loop that awaits it, and every
    # test gets a fresh loop from asyncio.run(). Reusing the module-level
    # events across tests makes .wait() raise "bound to a different event
    # loop" inside the waiter task, which asyncio.wait reports as *completed*
    # — the scheduler then spins instead of sleeping. Production has one loop
    # for the process lifetime (see _build_client), so this is test-only.
    monkeypatch.setattr(mod, "_ws_wake", asyncio.Event())
    monkeypatch.setattr(mod, "_news_wake", asyncio.Event())
    calls = {
        "build": 0, "cycle": 0, "news": 0, "register": 0,
        "disconnect": 0, "clients": [],
    }

    class FakeClient:
        def __init__(self):
            self.connected = True
            self._disconnected = asyncio.get_running_loop().create_future()

        def is_connected(self):
            return self.connected

        @property
        def disconnected(self):
            return asyncio.shield(self._disconnected)

        def fail_terminally(self):
            self.connected = False
            if not self._disconnected.done():
                self._disconnected.set_exception(
                    ConnectionError("Cannot send requests while disconnected")
                )

        async def disconnect(self):
            calls["disconnect"] += 1
            self.connected = False
            if not self._disconnected.done():
                self._disconnected.set_result(None)

    async def fake_build():
        calls["build"] += 1
        client = FakeClient()
        calls["clients"].append(client)
        return client
    monkeypatch.setattr(mod, "_build_client", fake_build)

    async def fake_register(client, _db):
        calls["register"] += 1
        return 3
    monkeypatch.setattr(mod, "register_source_handlers", fake_register)

    async def fake_cycle(client):
        calls["cycle"] += 1
        return False
    monkeypatch.setattr(mod, "_run_guarded_cycle", fake_cycle)

    async def fake_news():
        calls["news"] += 1
        mod._last_news_pass_ts = 0.0  # keep the throttle open for the test
    monkeypatch.setattr(mod, "_run_guarded_news_pass", fake_news)
    # run_loop writes the anchors directly (module globals, not monkeypatched)
    saved = (mod._last_vote_pass_ts, mod._last_news_pass_ts)
    try:
        yield mod, calls
    finally:
        for client in calls["clients"]:
            if client._disconnected.done() and not client._disconnected.cancelled():
                client._disconnected.exception()
        mod._last_vote_pass_ts, mod._last_news_pass_ts = saved
        mod._news_wake.clear()
        mod._ws_wake.clear()


def _drive_loop(mod, seconds):
    async def driver():
        task = asyncio.create_task(mod.run_loop())
        try:
            await asyncio.sleep(seconds)
        finally:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
    asyncio.run(asyncio.wait_for(driver(), timeout=10))


def test_run_loop_wakes_news_pass(loop_env):
    mod, calls = loop_env
    mod._news_wake.set()
    _drive_loop(mod, 0.5)
    assert calls["build"] == 1
    assert calls["register"] == 1
    assert calls["cycle"] >= 1
    assert calls["news"] >= 1


def test_run_loop_rebuilds_client_on_cycle_timeout(loop_env, monkeypatch):
    """A timed-out cycle rebuilds the client IN the timeout branch — push comes
    back within seconds — then falls through to the normal between-cycle wait.
    An aborted cycle must not immediately launch another full cycle."""
    mod, calls = loop_env
    mod._news_wake.clear()

    async def timing_out_cycle(client):
        calls["cycle"] += 1
        return calls["cycle"] == 1  # first cycle "times out" → rebuild expected
    monkeypatch.setattr(mod, "_run_guarded_cycle", timing_out_cycle)
    _drive_loop(mod, 0.5)
    assert calls["build"] == 2      # initial + prompt rebuild after timeout
    assert calls["register"] == 2   # handlers re-registered on the new client
    # once for the wedged transport, once for the rebuilt one at shutdown
    assert calls["disconnect"] == 2
    assert calls["cycle"] == 1      # CYCLE_INTERVAL wait, not a back-to-back cycle


def test_run_loop_disconnects_the_wedged_client(loop_env, monkeypatch):
    """The whole point of the rebuild: the old transport is presumed dead, so it
    is torn down rather than left holding a half-open Telegram session."""
    mod, calls = loop_env
    mod._news_wake.clear()

    async def timing_out_cycle(client):
        calls["cycle"] += 1
        return calls["cycle"] == 1
    monkeypatch.setattr(mod, "_run_guarded_cycle", timing_out_cycle)
    _drive_loop(mod, 0.5)
    assert calls["disconnect"] >= 1


def test_run_loop_rebuilds_after_fast_terminal_disconnect(
    loop_env, monkeypatch, caplog
):
    """Pin the August outage shape end to end: iter_messages reports the dead
    transport, fetch contains it, run_agent returns normally, and run_loop must
    still rebuild instead of logging successful empty cycles for another hour.
    """
    mod, calls = loop_env
    mod._news_wake.clear()
    monkeypatch.setattr(mod, "CHANNELS", ["@source"])
    monkeypatch.setattr(mod, "ID_SOURCES", {})
    monkeypatch.setattr(mod, "load_credentials", lambda: ("id", "hash", "key"))

    async def fake_resolve(client, channel, _db):
        return 111

    monkeypatch.setattr(mod, "resolve_channel", fake_resolve)
    ordinary_build = mod._build_client

    async def build_with_real_read_failure():
        client = await ordinary_build()

        def iter_messages(*_args, **_kwargs):
            async def disconnected_stream():
                client.fail_terminally()
                raise ConnectionError("Cannot send requests while disconnected")
                yield  # pragma: no cover - keeps this an async generator
            return disconnected_stream()

        client.iter_messages = iter_messages
        return client

    monkeypatch.setattr(mod, "_build_client", build_with_real_read_failure)
    normal_returns = []

    async def disconnected_cycle(client):
        calls["cycle"] += 1
        await mod.run_agent(client)
        normal_returns.append(True)
        return False

    monkeypatch.setattr(mod, "_run_guarded_cycle", disconnected_cycle)
    _drive_loop(mod, 1.0)

    assert calls["build"] == 2
    assert calls["register"] == 2
    assert calls["disconnect"] == 2
    assert calls["cycle"] == 1
    assert normal_returns == [True]
    assert "Cannot send requests while disconnected" in caplog.text


def test_run_loop_rebuilds_terminal_client_before_full_cycle(loop_env, monkeypatch):
    """Registration can finish just as Telethon exhausts reconnects. A client
    already known terminal must be replaced before any false-success scan."""
    mod, calls = loop_env
    ordinary_connect = mod._connect_and_register
    cycle_connection_states = []
    handoff = 0

    async def first_two_handoffs_are_already_terminal(push_db):
        nonlocal handoff
        client, disconnect_watch = await ordinary_connect(push_db)
        handoff += 1
        if client is not None and handoff <= 2:
            client.fail_terminally()
        return client, disconnect_watch

    async def observe_cycle_client(client):
        calls["cycle"] += 1
        cycle_connection_states.append(client.is_connected())
        return False

    monkeypatch.setattr(
        mod, "_connect_and_register", first_two_handoffs_are_already_terminal)
    monkeypatch.setattr(mod, "_run_guarded_cycle", observe_cycle_client)
    _drive_loop(mod, 0.4)

    assert calls["build"] == 3
    assert calls["register"] == 3
    assert cycle_connection_states == [True]


def test_run_loop_backs_off_when_every_connected_client_is_terminal(
    loop_env, monkeypatch
):
    """A connect that dies during registration is a failed attempt. Repeating
    that state must use the retry cadence instead of hammering Telegram and the
    shared session in a tight close/build loop."""
    mod, calls = loop_env
    monkeypatch.setattr(mod, "CONNECT_RETRY_INTERVAL", 0.05)
    ordinary_build = mod._build_client

    async def always_terminal_build():
        client = await ordinary_build()
        if calls["build"] > 20:
            raise AssertionError("terminal reconnect loop has no backoff")
        client.fail_terminally()
        return client

    monkeypatch.setattr(mod, "_build_client", always_terminal_build)
    _drive_loop(mod, 0.22)

    assert 2 <= calls["build"] <= 5
    assert calls["cycle"] == 0


def test_run_loop_rebuilds_on_terminal_disconnect_between_cycles(loop_env, monkeypatch):
    """A terminal Telethon failure must wake the between-cycle scheduler;
    waiting for the next hourly cycle leaves push ingestion dead."""
    mod, calls = loop_env
    mod._news_wake.clear()

    async def cycle_then_disconnect(client):
        calls["cycle"] += 1
        asyncio.get_running_loop().call_later(0.05, client.fail_terminally)
        return False

    monkeypatch.setattr(mod, "_run_guarded_cycle", cycle_then_disconnect)
    _drive_loop(mod, 0.5)

    assert calls["build"] == 2
    assert calls["register"] == 2
    assert calls["disconnect"] == 2
    assert calls["cycle"] == 1


def test_cycle_disconnect_waits_for_telethon_terminal_cleanup(loop_env, monkeypatch):
    """is_connected() turns false before Telethon's terminal sender cleanup
    completes. Client-level disconnect must not race that in-flight cleanup."""
    mod, calls = loop_env
    ordinary_build = mod._build_client
    cleanup_finished_before_close = []

    async def build_with_order_probe():
        client = await ordinary_build()
        ordinary_disconnect = client.disconnect

        async def ordered_disconnect():
            cleanup_finished_before_close.append(client._disconnected.done())
            await ordinary_disconnect()

        client.disconnect = ordered_disconnect
        return client

    async def disconnect_during_cycle(client):
        calls["cycle"] += 1
        client.connected = False

        def finish_terminal_cleanup():
            if not client._disconnected.done():
                client._disconnected.set_exception(
                    ConnectionError("Cannot send requests while disconnected")
                )

        asyncio.get_running_loop().call_later(0.05, finish_terminal_cleanup)
        return False

    monkeypatch.setattr(mod, "_build_client", build_with_order_probe)
    monkeypatch.setattr(mod, "_run_guarded_cycle", disconnect_during_cycle)
    _drive_loop(mod, 0.4)

    assert calls["build"] == 2
    assert cleanup_finished_before_close == [True, False]


def test_run_loop_retries_after_failed_cycle_disconnect_rebuild(loop_env, monkeypatch):
    """A transient failure rebuilding the dead client gets the connect retry
    cadence, not the remaining full-cycle delay."""
    mod, calls = loop_env
    mod._news_wake.clear()
    monkeypatch.setattr(mod, "CONNECT_RETRY_INTERVAL", 0.05, raising=False)
    successful_build = mod._build_client
    attempt = 0

    async def flaky_build():
        nonlocal attempt
        attempt += 1
        if attempt == 2:
            calls["build"] += 1
            raise ConnectionError("DNS still unavailable")
        return await successful_build()

    async def disconnect_first_cycle(client):
        calls["cycle"] += 1
        if calls["cycle"] == 1:
            client.fail_terminally()
        return False

    monkeypatch.setattr(mod, "_build_client", flaky_build)
    monkeypatch.setattr(mod, "_run_guarded_cycle", disconnect_first_cycle)
    _drive_loop(mod, 0.4)

    assert calls["build"] == 3       # initial, failed rebuild, retry succeeds
    assert calls["register"] == 2    # initial client and recovered client
    assert calls["cycle"] == 2       # recovered without waiting CYCLE_INTERVAL


def test_run_loop_retries_after_failed_between_cycle_rebuild(loop_env, monkeypatch):
    """The same bounded retry applies when the watcher, rather than the
    post-cycle state check, discovers the terminal client."""
    mod, calls = loop_env
    monkeypatch.setattr(mod, "CONNECT_RETRY_INTERVAL", 0.05)
    successful_build = mod._build_client
    attempt = 0

    async def flaky_build():
        nonlocal attempt
        attempt += 1
        if attempt == 2:
            calls["build"] += 1
            raise ConnectionError("DNS still unavailable")
        return await successful_build()

    async def first_cycle_then_disconnect(client):
        calls["cycle"] += 1
        if calls["cycle"] == 1:
            asyncio.get_running_loop().call_later(0.02, client.fail_terminally)
        return False

    monkeypatch.setattr(mod, "_build_client", flaky_build)
    monkeypatch.setattr(mod, "_run_guarded_cycle", first_cycle_then_disconnect)
    _drive_loop(mod, 0.5)

    assert calls["build"] == 3
    assert calls["register"] == 2
    assert calls["cycle"] == 2


def test_terminal_disconnect_preempts_continuously_ready_mini_pass(
    loop_env, monkeypatch
):
    """Fresh WS work cannot starve terminal-client recovery indefinitely."""
    mod, calls = loop_env
    monkeypatch.setattr(mod, "ENABLE_WS_MINI_PASS", True)
    monkeypatch.setattr(mod, "MINI_PASS_MIN_INTERVAL", 0)
    mini_calls = 0

    async def cycle_then_disconnect(client):
        calls["cycle"] += 1
        asyncio.get_running_loop().call_later(0.01, client.fail_terminally)
        return False

    async def always_refilled_mini_pass():
        nonlocal mini_calls
        mini_calls += 1
        await asyncio.sleep(0.02)  # terminal failure lands while work is running
        mod._last_vote_pass_ts = 0.0
        if calls["build"] == 1:
            mod._ws_wake.set()     # another pass is immediately runnable

    monkeypatch.setattr(mod, "_run_guarded_cycle", cycle_then_disconnect)
    monkeypatch.setattr(mod, "_run_guarded_mini_pass", always_refilled_mini_pass)
    mod._ws_wake.set()
    _drive_loop(mod, 0.4)

    assert mini_calls >= 1
    assert calls["build"] == 2
    assert calls["register"] == 2


def test_shutdown_failure_neither_replaces_nor_recloses_client(loop_env, monkeypatch):
    """A failed Telethon cleanup is fatal. Rebuilding or calling disconnect a
    second time would create overlapping shielded cleanup against one session.
    """
    mod, calls = loop_env
    ordinary_build = mod._build_client
    shutdown_attempts = 0

    async def build_with_broken_shutdown():
        nonlocal shutdown_attempts
        client = await ordinary_build()
        if calls["build"] == 1:
            async def broken_disconnect():
                nonlocal shutdown_attempts
                shutdown_attempts += 1
                raise OSError("session flush failed")
            client.disconnect = broken_disconnect
        return client

    async def terminal_cycle(client):
        calls["cycle"] += 1
        client.fail_terminally()
        return False

    monkeypatch.setattr(mod, "_build_client", build_with_broken_shutdown)
    monkeypatch.setattr(mod, "_run_guarded_cycle", terminal_cycle)
    _drive_loop(mod, 0.3)

    assert calls["build"] == 1
    assert shutdown_attempts == 1


def test_run_loop_push_disabled_registers_nothing(loop_env, monkeypatch):
    mod, calls = loop_env
    monkeypatch.setattr(mod, "ENABLE_SOURCE_PUSH", False)
    mod._news_wake.set()
    _drive_loop(mod, 0.4)
    assert calls["register"] == 0 and calls["news"] == 0


def test_run_loop_retries_when_client_build_fails(loop_env, monkeypatch):
    """A failed connect must not spin: no cycle runs and no handler registers
    until a client exists."""
    mod, calls = loop_env

    async def failing_build():
        calls["build"] += 1
        raise RuntimeError("connect refused")
    monkeypatch.setattr(mod, "_build_client", failing_build)
    _drive_loop(mod, 0.3)
    assert calls["build"] == 1      # backoff, not a hot retry loop
    assert calls["cycle"] == 0 and calls["register"] == 0


def test_run_loop_disconnects_client_when_registration_fails(loop_env, monkeypatch):
    """A started client whose handler registration blew up is already holding a
    live Telegram session — dropping the reference would leak one per retry."""
    mod, calls = loop_env

    async def failing_register(client, _db):
        calls["register"] += 1
        raise RuntimeError("resolve storm")
    monkeypatch.setattr(mod, "register_source_handlers", failing_register)
    _drive_loop(mod, 0.3)
    assert calls["build"] == 1 and calls["register"] == 1
    assert calls["disconnect"] == 1
    assert calls["cycle"] == 0


def test_run_loop_runs_the_more_overdue_pass_first(loop_env, monkeypatch):
    """Starvation pin: a fixed vote-before-news order let a long vote pass
    return with its own gate already reopened and _ws_wake re-set by the
    listener, so it won every iteration and news never ran for the whole
    between-cycle window. The more-overdue pass goes first instead."""
    mod, calls = loop_env
    monkeypatch.setattr(mod, "ENABLE_WS_MINI_PASS", True)
    monkeypatch.setattr(mod, "MINI_PASS_MIN_INTERVAL", 0)
    monkeypatch.setattr(mod, "NEWS_PASS_MIN_INTERVAL", 0)
    order = []

    async def fake_mini():
        order.append("vote")
        mod._last_vote_pass_ts = time.time()
        if len(order) < 3:
            mod._ws_wake.set()   # a fresh WS event lands while the pass runs
    monkeypatch.setattr(mod, "_run_guarded_mini_pass", fake_mini)

    async def fake_news():
        order.append("news")
        mod._last_news_pass_ts = time.time()
    monkeypatch.setattr(mod, "_run_guarded_news_pass", fake_news)

    mod._ws_wake.set()
    mod._news_wake.set()
    _drive_loop(mod, 0.3)
    # Tie on the first iteration keeps vote first; the second must be news,
    # whose anchor is now the older (more overdue) of the two.
    assert order[:2] == ["vote", "news"]


def test_run_loop_throttles_news_pass(loop_env, monkeypatch):
    """A set wake inside the throttle window must not fire the pass early."""
    mod, calls = loop_env
    monkeypatch.setattr(mod, "NEWS_PASS_MIN_INTERVAL", 60)

    async def fake_news():
        calls["news"] += 1
    monkeypatch.setattr(mod, "_run_guarded_news_pass", fake_news)
    mod._news_wake.set()
    _drive_loop(mod, 0.3)
    assert calls["cycle"] >= 1 and calls["news"] == 0


def test_guarded_cycle_returns_timeout_flag(agent, monkeypatch):
    mod, db = agent
    monkeypatch.setattr(mod, "CYCLE_DEADLINE", 0.05)

    async def hung_agent(client):
        await asyncio.sleep(1.0)
    monkeypatch.setattr(mod, "run_agent", hung_agent)
    assert asyncio.run(mod._run_guarded_cycle(object())) is True

    async def failing_agent(client):
        raise RuntimeError("cycle error")
    monkeypatch.setattr(mod, "run_agent", failing_agent)
    monkeypatch.setattr(mod, "CYCLE_DEADLINE", 5)
    assert asyncio.run(mod._run_guarded_cycle(object())) is False


def test_guarded_cycle_passes_the_client_through(agent, monkeypatch):
    mod, db = agent
    monkeypatch.setattr(mod, "CYCLE_DEADLINE", 5)
    seen = {}

    async def spy_agent(client):
        seen["client"] = client
    monkeypatch.setattr(mod, "run_agent", spy_agent)
    sentinel = object()
    assert asyncio.run(mod._run_guarded_cycle(sentinel)) is False
    assert seen["client"] is sentinel


# ── ID-keyed sources (private channels/groups, push + hourly) ─────────────

def test_id_sources_config_sane():
    mod = _load_agent_module()
    assert isinstance(mod.ID_SOURCES, dict) and mod.ID_SOURCES
    labels = list(mod.ID_SOURCES.values())
    # Labels key cursors, evaluated_messages, and the push queue — collisions
    # or leading/trailing junk would corrupt per-source state.
    assert len(labels) == len(set(labels))
    assert all(isinstance(i, int) and i < 0 for i in mod.ID_SOURCES)
    assert all(lbl and lbl.strip() == lbl for lbl in labels)
    # ID_SOURCES is env JSON; string keys become marked int ids, and the
    # exact label (which keys the cursor row) is preserved.
    assert mod.ID_SOURCES == {-1001000000777: "Example Private Feed"}


def test_read_id_sources_fetches_by_id_with_label(agent, monkeypatch):
    mod, db = agent

    class ChanEntity:
        megagroup = False

    class FakeClient:
        def __init__(self):
            self.requested = []

        async def get_entity(self, src_id):
            self.requested.append(src_id)
            if src_id == -100999:
                raise RuntimeError("not joined")
            return ChanEntity()

    async def fake_fetch(client, entity, min_id=0, limit=50, since=None,
                         channel_name=None, is_group=False):
        assert min_id == db.get_cursor(channel_name)
        return [{"channel": channel_name, "id": 7,
                 "text": "Breaking: https://a.com/x",
                 "date": "2026-08-02T10:00:00+00:00", "is_group": is_group}]
    monkeypatch.setattr(mod, "fetch_channel_messages", fake_fetch)
    monkeypatch.setattr(mod, "ID_SOURCES", {-100111: "Example Security Alerts",
                                            -100999: "Broken Source"})
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)
        assert len(sleeps) < 20, "runaway pacing loop"
    monkeypatch.setattr(mod.asyncio, "sleep", fake_sleep)
    client = FakeClient()
    msgs = asyncio.run(mod.read_id_sources(client, db, since=None))
    assert [m["channel"] for m in msgs] == ["Example Security Alerts"]
    assert db.get_cursor("Example Security Alerts") == 7
    assert -100999 in client.requested  # failed source skipped, not fatal


def test_register_covers_id_sources_with_label_routing(agent, monkeypatch):
    mod, db = agent
    db.save_channel_id("@one", 1000000111, "One", "channel")
    monkeypatch.setattr(mod, "CHANNELS", ["@one"])
    monkeypatch.setattr(mod, "ID_SOURCES", {-100222: "Example Security Alerts"})

    class GroupEntity:
        megagroup = True

    class FakeClient:
        def __init__(self):
            self.registered = {}

        async def get_entity(self, src_id):
            return GroupEntity()

        def add_event_handler(self, cb, event_filter):
            self.registered.setdefault("cb", cb)
            self.registered.setdefault("filter", event_filter)

    async def fake_resolve(client, channel, _db):
        return _db.get_channel_id(channel)
    monkeypatch.setattr(mod, "resolve_channel", fake_resolve)
    client = FakeClient()
    n = asyncio.run(mod.register_source_handlers(client, db))
    assert n == 2
    from telethon import utils
    from telethon.tl import types
    marked_one = utils.get_peer_id(types.PeerChannel(1000000111))
    assert sorted(client.registered["filter"].chats) == sorted([marked_one, -100222])
    # An id-source event routes by dialog id and stores the LABEL as the
    # channel key; is_group=True (megagroup) drops text-only messages, so a
    # URL-bearing one must pass.
    mod._news_wake.clear()
    ev = SimpleNamespace(
        chat_id=-100222,
        message=SimpleNamespace(id=9, text="Security alert https://a.com/x",
                                date=datetime(2026, 8, 2, 10, 0,
                                              tzinfo=timezone.utc)))
    asyncio.run(client.registered["cb"](ev))
    rows = db.get_pending_source_messages(limit=10)
    assert [(r["channel"], r["msg_id"]) for r in rows] == [("Example Security Alerts", 9)]
    assert mod._news_wake.is_set()
