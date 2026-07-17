"""Tests for the Leviathan live-news WS event queue (ws_events table + listener plumbing)."""

import asyncio
import importlib.util
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


def _load_agent_module():
    """Import ln-agent.py under a Python-safe module name (hyphen in filename)."""
    if "ln_agent_ws_test" in sys.modules:
        return sys.modules["ln_agent_ws_test"]
    spec = importlib.util.spec_from_file_location(
        "ln_agent_ws_test", ROOT / "ln-agent.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ln_agent_ws_test"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def agent(tmp_path):
    mod = _load_agent_module()
    db = mod.AgentDB(db_path=tmp_path / "test.db")
    yield mod, db
    db.close()


def test_add_ws_event_inserts_and_dedups(agent):
    mod, db = agent
    assert db.add_ws_event("news.approved", 101, "slug-a", "Headline A",
                           "2026-07-02T10:00:00Z", "manual", "{}") is True
    # Same (news_id, event_type) again -> ignored
    assert db.add_ws_event("news.approved", 101, "slug-a", "Headline A",
                           "2026-07-02T10:00:00Z", "manual", "{}") is False
    # Different event type, same id -> new row
    assert db.add_ws_event("news.tsunami_promoted", 101, "slug-a", "Headline A",
                           "2026-07-02T10:00:00Z", "manual", "{}") is True


def test_consumer_flags_are_independent(agent):
    mod, db = agent
    db.add_ws_event("news.approved", 1, None, "H1", None, None, None)
    db.add_ws_event("news.approved", 2, None, "H2", None, None, None)

    rows = db.get_unconsumed_ws_events("agent")
    assert [r["news_id"] for r in rows] == [1, 2]

    db.mark_ws_events_consumed("agent", [rows[0]["id"]])
    assert [r["news_id"] for r in db.get_unconsumed_ws_events("agent")] == [2]
    # Bot flag untouched by agent consumption
    assert [r["news_id"] for r in db.get_unconsumed_ws_events("bot")] == [1, 2]


def test_get_unconsumed_respects_limit_and_order(agent):
    mod, db = agent
    for i in range(5):
        db.add_ws_event("news.approved", 100 + i, None, f"H{i}", None, None, None)
    rows = db.get_unconsumed_ws_events("agent", limit=3)
    assert [r["news_id"] for r in rows] == [100, 101, 102]


def test_mark_consumed_empty_list_is_noop(agent):
    mod, db = agent
    db.mark_ws_events_consumed("agent", [])  # must not raise


def test_invalid_consumer_rejected(agent):
    mod, db = agent
    with pytest.raises(ValueError):
        db.get_unconsumed_ws_events("evil; DROP TABLE ws_events")
    with pytest.raises(ValueError):
        db.mark_ws_events_consumed("evil", [1])


def test_prune_ws_events(agent):
    mod, db = agent
    db.add_ws_event("news.approved", 1, None, "old", None, None, None)
    old = (datetime.now(timezone.utc) - timedelta(days=9)).isoformat()
    db._execute_commit("UPDATE ws_events SET received_at = ? WHERE news_id = 1", (old,))
    db.add_ws_event("news.approved", 2, None, "fresh", None, None, None)

    assert db.prune_ws_events(days=7) == 1
    assert [r["news_id"] for r in db.get_unconsumed_ws_events("agent")] == [2]


# ─── Frame handler (Task 2) ──────────────────────────────────────────────────

def _frame(events):
    return json.dumps({"events": events, "t": 1750000000})


def _evt(etype="news.approved", nid=1, **kw):
    d = {"type": etype, "id": nid, "slug": f"slug-{nid}", "status": "approved",
         "headline": f"Headline {nid}", "date_posted": "2026-07-02T10:00:00Z",
         "origin": "manual", "previous_status": "submitted", "sponsored": None}
    d.update(kw)
    return d


@pytest.fixture
def fresh_ws_state(agent):
    """Reset wake/gap/connection globals around each frame-handler test."""
    mod, db = agent
    mod._ws_wake.clear()
    mod._clear_ws_gap()
    mod._ws_connected_at = None
    yield mod, db
    mod._ws_wake.clear()
    mod._clear_ws_gap()
    mod._ws_connected_at = None


def test_frame_heartbeat_ignored(fresh_ws_state):
    mod, db = fresh_ws_state
    assert mod._handle_ws_frame('{"type": "heartbeat"}', db) == 0
    assert not mod._ws_wake.is_set()
    assert not mod.ws_gap_set()


def test_frame_reconcile_sets_gap_and_wake(fresh_ws_state):
    mod, db = fresh_ws_state
    assert mod._handle_ws_frame('{"type": "reconcile", "reason": "backlog"}', db) == 0
    assert mod.ws_gap_set()
    assert mod._ws_wake.is_set()


def test_frame_batch_inserts_and_wakes(fresh_ws_state):
    mod, db = fresh_ws_state
    raw = _frame([_evt(nid=201), _evt(nid=202)])
    assert mod._handle_ws_frame(raw, db) == 2
    assert mod._ws_wake.is_set()
    assert [r["news_id"] for r in db.get_unconsumed_ws_events("agent")] == [201, 202]


def test_frame_filters_event_types(fresh_ws_state):
    mod, db = fresh_ws_state
    raw = _frame([_evt("news.tsunami", 301), _evt("news.sponsored", 302),
                  _evt("news.approved", 303)])
    assert mod._handle_ws_frame(raw, db) == 1
    assert [r["news_id"] for r in db.get_unconsumed_ws_events("agent")] == [303]
    # nothing new -> wake still set from the approved event only
    assert mod._ws_wake.is_set()


def test_frame_duplicate_events_do_not_rewake(fresh_ws_state):
    mod, db = fresh_ws_state
    raw = _frame([_evt(nid=401)])
    assert mod._handle_ws_frame(raw, db) == 1
    mod._ws_wake.clear()
    assert mod._handle_ws_frame(raw, db) == 0
    assert not mod._ws_wake.is_set()


@pytest.mark.parametrize("raw", [
    "not json at all",
    '"just a string"',
    '{"events": "not-a-list"}',
    json.dumps({"events": [{"type": "news.approved", "id": "not-an-int"}]}),
    json.dumps({"events": ["not-a-dict"]}),
])
def test_frame_malformed_input_is_safe(fresh_ws_state, raw):
    mod, db = fresh_ws_state
    assert mod._handle_ws_frame(raw, db) == 0
    assert db.get_unconsumed_ws_events("agent") == []


# ─── Listener supervisor (Task 3) ────────────────────────────────────────────

def _capped_sleep(calls, cap=20):
    """Zero-delay sleep stub with a HARD iteration bound.

    SAFETY: any supervisor test that stubs out sleep MUST bound its loop from
    inside the stub itself — if the fake listener ever fails to reach its own
    stop condition (e.g. it raises before the guard line), the supervisor
    retries at CPU speed and the sleeps list grows until RAM dies. This
    exact failure crashed a 50GB workstation on 2026-07-02.
    """
    async def fake_sleep(secs):
        calls["sleeps"].append(secs)
        if len(calls["sleeps"]) > cap:
            raise asyncio.CancelledError(f"fake_sleep cap ({cap}) exceeded — runaway loop")
    return fake_sleep


def test_listener_supervisor_backs_off_and_retries(fresh_ws_state, monkeypatch):
    mod, db = fresh_ws_state
    calls = {"listen": 0, "sleeps": []}

    async def fake_listen_once():
        calls["listen"] += 1
        if calls["listen"] >= 3:
            raise asyncio.CancelledError  # GUARD FIRST — must precede any other statement
        raise ConnectionError("boom")

    monkeypatch.setattr(mod, "_ws_listen_once", fake_listen_once)
    monkeypatch.setattr(mod.asyncio, "sleep", _capped_sleep(calls))
    mod._clear_ws_gap()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(mod._ws_listener_supervisor())

    assert calls["listen"] == 3
    assert len(calls["sleeps"]) == 2          # slept between attempts
    assert calls["sleeps"][1] > calls["sleeps"][0]  # exponential growth
    assert mod.ws_gap_set()                   # every disconnect flags a gap


def test_listener_supervisor_disables_without_dep(fresh_ws_state, monkeypatch):
    mod, db = fresh_ws_state

    async def fake_listen_once():
        raise ImportError("No module named 'websockets'")

    monkeypatch.setattr(mod, "_ws_listen_once", fake_listen_once)
    # Must return cleanly (listener disabled), not loop forever.
    asyncio.run(mod._ws_listener_supervisor())


def test_listener_supervisor_resets_backoff_after_stable_connection(fresh_ws_state, monkeypatch):
    """A connection that lived >= WS_STABLE_SECONDS resets backoff to base —
    a flaky-but-working stream must not ratchet to permanent 300s gaps
    (observed in production 2026-07-02: 20->40->80->160->300 while every
    reconnect was actually succeeding)."""
    mod, db = fresh_ws_state
    calls = {"listen": 0, "sleeps": []}
    # getattr with default: in the red phase (constant not implemented yet) the
    # fake must still terminate — never let a missing attribute raise here.
    stable = getattr(mod, "WS_STABLE_SECONDS", 60)

    async def fake_listen_once():
        calls["listen"] += 1
        if calls["listen"] >= 3:
            raise asyncio.CancelledError  # GUARD FIRST — must precede any other statement
        # Simulate: connection established and lived well past the stability bar.
        mod._ws_connected_at = time.time() - (stable + 60)
        raise ConnectionError("stall")

    monkeypatch.setattr(mod, "_ws_listen_once", fake_listen_once)
    monkeypatch.setattr(mod.asyncio, "sleep", _capped_sleep(calls))

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(mod._ws_listener_supervisor())

    # Both sleeps start from base backoff (+ jitter <= base/4), never doubled.
    cap = mod.WS_BACKOFF_BASE * 1.25 + 0.01
    assert all(s <= cap for s in calls["sleeps"]), calls["sleeps"]


# ─── vote_comment_pass extraction (Task 4) ───────────────────────────────────

class _StubLN:
    """Minimal LNClient stand-in for vote_comment_pass."""

    def __init__(self):
        self.user_id = 999
        self.votes = []
        self.yaps_posted = []

    def vote(self, target_id, weight, label=""):
        self.votes.append((target_id, weight))

    def has_our_comment(self, article_id):
        return False

    def post_yap(self, article_id, text, tags=None):
        self.yaps_posted.append((article_id, text))

    def get_yaps(self, article_id):
        return []


@pytest.fixture
def pass_env(agent, monkeypatch):
    """Neutralize LLM + reply calls inside vote_comment_pass."""
    mod, db = agent
    monkeypatch.setattr(mod, "batch_evaluate_articles", lambda arts: {a["id"]: 1 for a in arts})
    monkeypatch.setattr(mod, "batch_evaluate_comments", lambda yaps: {})
    monkeypatch.setattr(mod, "craft_comment", lambda h, t, u="": "solid take with substance here")
    monkeypatch.setattr(mod, "walk_replies_and_respond", lambda *a, **k: None)

    async def no_sleep(_):
        return None
    monkeypatch.setattr(mod.asyncio, "sleep", no_sleep)
    return mod, db


def _article(aid, headline="H", created="2026-07-02T10:00:00Z"):
    return {"id": aid, "headline": headline, "content_type": "news",
            "created_at": created, "tags": [], "author": {"username": "someone"},
            "url": f"https://example.com/{aid}"}


def test_vote_comment_pass_since_none_processes_old_articles(pass_env):
    mod, db = pass_env
    ln = _StubLN()
    arts = [_article(1, created="2020-01-01T00:00:00Z")]
    voted, commented, processed = asyncio.run(
        mod.vote_comment_pass(ln, db, arts, since=None))
    assert voted == 1 and commented == 1
    assert processed == {1}
    assert ln.votes == [(1, 1)]


def test_vote_comment_pass_respects_since_filter(pass_env):
    mod, db = pass_env
    ln = _StubLN()
    arts = [_article(2, created="2020-01-01T00:00:00Z")]
    since = datetime(2026, 1, 1, tzinfo=timezone.utc)
    voted, commented, processed = asyncio.run(
        mod.vote_comment_pass(ln, db, arts, since=since))
    assert (voted, commented) == (0, 0)
    assert processed == set()


def test_vote_comment_pass_is_idempotent_via_db(pass_env):
    mod, db = pass_env
    ln = _StubLN()
    arts = [_article(3)]
    asyncio.run(mod.vote_comment_pass(ln, db, arts, since=None))
    asyncio.run(mod.vote_comment_pass(ln, db, arts, since=None))
    assert len(ln.votes) == 1          # second pass: was_article_voted -> skip
    assert len(ln.yaps_posted) == 1    # second pass: was_commented -> skip


# ─── Mini-pass + run_loop (Task 5) ───────────────────────────────────────────

class _NonClosingDB:
    """Wrap a live AgentDB so run_mini_pass's close() doesn't kill the fixture db."""

    def __init__(self, real):
        self._real = real

    def __getattr__(self, name):
        if name == "close":
            return lambda: None
        return getattr(self._real, name)


class _MiniLN:
    """LNClient stand-in for run_mini_pass wiring tests."""

    def __init__(self, key, recent=None):
        self._recent = recent or []

    def authenticate(self):
        return True

    def get_recent_articles(self, per_page=20):
        return self._recent


def test_run_mini_pass_no_events_is_cheap(fresh_ws_state, monkeypatch):
    mod, db = fresh_ws_state

    def boom(*a, **k):
        raise AssertionError("LNClient must not be constructed with empty queue")

    monkeypatch.setattr(mod, "AgentDB", lambda: _NonClosingDB(db))
    monkeypatch.setattr(mod, "load_credentials", lambda: ("id", "hash", "key"))
    monkeypatch.setattr(mod, "LNClient", boom)

    asyncio.run(mod.run_mini_pass())  # must not raise


def test_run_mini_pass_processes_queued_ids_only(fresh_ws_state, monkeypatch):
    mod, db = fresh_ws_state
    db.add_ws_event("news.approved", 11, None, "Q1", None, None, None)
    mod._clear_ws_gap()

    recent = [_article(11), _article(12)]
    passed = {}

    async def fake_pass(ln, adb, articles, since):
        passed["articles"] = articles
        passed["since"] = since
        return 1, 0, {a["id"] for a in articles}

    monkeypatch.setattr(mod, "AgentDB", lambda: _NonClosingDB(db))
    monkeypatch.setattr(mod, "load_credentials", lambda: ("id", "hash", "key"))
    monkeypatch.setattr(mod, "LNClient", lambda key: _MiniLN(key, recent))
    monkeypatch.setattr(mod, "vote_comment_pass", fake_pass)

    asyncio.run(mod.run_mini_pass())

    assert [a["id"] for a in passed["articles"]] == [11]     # queued id only
    assert passed["since"] is None
    assert db.get_unconsumed_ws_events("agent") == []        # drained rows marked


def test_run_mini_pass_ignores_gap_processes_queued_only(fresh_ws_state, monkeypatch):
    """Gap no longer widens the mini-pass — backfill is the hourly cycle's job.

    A gap-widened pass over the full recent feed is full-Phase-4-sized work and
    blew the 600s deadline in production (2026-07-02). The mini-pass now does
    strictly bounded, queued-ids-only work regardless of the gap flag.
    """
    mod, db = fresh_ws_state
    db.add_ws_event("news.approved", 21, None, "Q", None, None, None)
    mod._set_ws_gap()

    recent = [_article(21), _article(22), _article(23)]
    passed = {}

    async def fake_pass(ln, adb, articles, since):
        passed["articles"] = articles
        return 0, 0, set()

    monkeypatch.setattr(mod, "AgentDB", lambda: _NonClosingDB(db))
    monkeypatch.setattr(mod, "load_credentials", lambda: ("id", "hash", "key"))
    monkeypatch.setattr(mod, "LNClient", lambda key: _MiniLN(key, recent))
    monkeypatch.setattr(mod, "vote_comment_pass", fake_pass)

    asyncio.run(mod.run_mini_pass())

    assert [a["id"] for a in passed["articles"]] == [21]  # queued only, gap or not
    assert mod.ws_gap_set()  # gap stays set for the full cycle to clear
    assert db.get_unconsumed_ws_events("agent") == []


def test_run_mini_pass_consumes_on_drain_even_when_pass_fails(fresh_ws_state, monkeypatch):
    """Events are marked consumed at drain time, BEFORE the LLM work — a
    deadline abort must not leave rows re-draining every subsequent pass
    (dedup tables + the hourly cycle cover any articles the abort skipped)."""
    mod, db = fresh_ws_state
    db.add_ws_event("news.approved", 31, None, "Q", None, None, None)
    seen_during_pass = {}

    async def fake_pass(ln, adb, articles, since):
        # By the time the expensive work starts, the queue must already be retired.
        seen_during_pass["unconsumed"] = list(db.get_unconsumed_ws_events("agent"))
        raise RuntimeError("simulated mid-pass failure/timeout")

    monkeypatch.setattr(mod, "AgentDB", lambda: _NonClosingDB(db))
    monkeypatch.setattr(mod, "load_credentials", lambda: ("id", "hash", "key"))
    monkeypatch.setattr(mod, "LNClient", lambda key: _MiniLN(key, [_article(31)]))
    monkeypatch.setattr(mod, "vote_comment_pass", fake_pass)

    with pytest.raises(RuntimeError):
        asyncio.run(mod.run_mini_pass())

    assert seen_during_pass["unconsumed"] == []           # consumed before the pass
    assert db.get_unconsumed_ws_events("agent") == []     # and still consumed after


def test_run_mini_pass_caps_targets_per_pass(fresh_ws_state, monkeypatch):
    mod, db = fresh_ws_state
    for i in range(8):
        db.add_ws_event("news.approved", 500 + i, None, f"Q{i}", None, None, None)
    recent = [_article(500 + i) for i in range(8)]
    passed = {}

    async def fake_pass(ln, adb, articles, since):
        passed["articles"] = articles
        return 0, 0, set()

    monkeypatch.setattr(mod, "AgentDB", lambda: _NonClosingDB(db))
    monkeypatch.setattr(mod, "load_credentials", lambda: ("id", "hash", "key"))
    monkeypatch.setattr(mod, "LNClient", lambda key: _MiniLN(key, recent))
    monkeypatch.setattr(mod, "vote_comment_pass", fake_pass)
    monkeypatch.setattr(mod, "MINI_PASS_MAX_ARTICLES", 5)

    asyncio.run(mod.run_mini_pass())

    assert len(passed["articles"]) == 5                    # capped
    assert db.get_unconsumed_ws_events("agent") == []      # but ALL drained rows retired


@pytest.fixture
def agent_module_isolated():
    """Module handle + restore of loop-related globals mutated by run_loop tests."""
    mod = _load_agent_module()
    saved = {n: getattr(mod, n) for n in
             ("CYCLE_INTERVAL", "MINI_PASS_MIN_INTERVAL", "ENABLE_WS_EVENTS",
              "ENABLE_WS_MINI_PASS", "_last_pass_ts")}
    mod._ws_wake.clear()
    try:
        yield mod
    finally:
        for n, v in saved.items():
            setattr(mod, n, v)
        mod._ws_wake.clear()


def test_run_loop_wake_triggers_mini_pass_then_full_cycle(agent_module_isolated, monkeypatch):
    """One wake -> one mini-pass -> then the timeout path reaches the next full cycle."""
    mod = agent_module_isolated
    calls = {"cycle": 0, "mini": 0}

    async def fake_cycle():
        calls["cycle"] += 1
        if calls["cycle"] >= 2:
            raise KeyboardInterrupt  # unwind run_loop after the 2nd full cycle

    async def fake_mini():
        calls["mini"] += 1

    monkeypatch.setattr(mod, "_run_guarded_cycle", fake_cycle)
    monkeypatch.setattr(mod, "_run_guarded_mini_pass", fake_mini)
    monkeypatch.setattr(mod, "ENABLE_WS_EVENTS", False)   # no real listener task
    monkeypatch.setattr(mod, "ENABLE_WS_MINI_PASS", True)
    monkeypatch.setattr(mod, "CYCLE_INTERVAL", 0.2)
    monkeypatch.setattr(mod, "MINI_PASS_MIN_INTERVAL", 0)

    mod._ws_wake.set()  # pending wake before the loop starts sleeping

    with pytest.raises(KeyboardInterrupt):
        asyncio.run(mod.run_loop())

    assert calls["cycle"] == 2
    assert calls["mini"] >= 1


def test_run_loop_mini_pass_disabled_sleeps_straight_through(agent_module_isolated, monkeypatch):
    mod = agent_module_isolated
    calls = {"cycle": 0, "mini": 0}

    async def fake_cycle():
        calls["cycle"] += 1
        if calls["cycle"] >= 2:
            raise KeyboardInterrupt

    async def fake_mini():
        calls["mini"] += 1

    monkeypatch.setattr(mod, "_run_guarded_cycle", fake_cycle)
    monkeypatch.setattr(mod, "_run_guarded_mini_pass", fake_mini)
    monkeypatch.setattr(mod, "ENABLE_WS_EVENTS", False)
    monkeypatch.setattr(mod, "ENABLE_WS_MINI_PASS", False)
    monkeypatch.setattr(mod, "CYCLE_INTERVAL", 0.05)

    mod._ws_wake.set()  # even with a pending wake, disabled mini-pass never runs

    with pytest.raises(KeyboardInterrupt):
        asyncio.run(mod.run_loop())

    assert calls["cycle"] == 2
    assert calls["mini"] == 0
