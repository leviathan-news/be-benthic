"""Independent catch-up, engagement, replies, and maintenance in Jev mode."""

import asyncio
from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def agent(tmp_path, monkeypatch):
    monkeypatch.setenv("JEV_TASKS", "news,votes")
    spec = importlib.util.spec_from_file_location("realtime_schedule_test", ROOT / "ln-agent.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    db = mod.AgentDB(tmp_path / "agent.db")
    monkeypatch.setattr(mod, "AgentDB", lambda: type(db)(db.db_path))
    monkeypatch.setattr(mod, "JEV_REALTIME", True)
    monkeypatch.setattr(mod, "CHANNELS", ["@source"])
    monkeypatch.setattr(mod, "ID_SOURCES", {})
    monkeypatch.setattr(mod, "ENABLE_MARKET_MATCH", False)
    yield mod, db
    db.close()


def test_catchup_only_enqueues_missing_news_and_never_authenticates_or_evaluates(agent, monkeypatch):
    mod, db = agent
    stamp = datetime.now(timezone.utc).isoformat()
    messages = [{"channel": "@source", "id": mid, "date": stamp,
                 "text": f"Protocol announces a launch https://example.com/{mid}", "is_group": False}
                for mid in (1, 2)]
    db.add_pending_source_message("@source", 1, messages[0]["text"], stamp)
    db.save_channel_id("@source", 123, "Source", "channel")
    async def resolve(*a): return 123
    async def fetch(*a, **k): return messages
    async def no_sleep(*a): pass
    monkeypatch.setattr(mod, "resolve_channel", resolve)
    monkeypatch.setattr(mod, "fetch_channel_messages", fetch)
    monkeypatch.setattr(mod.asyncio, "sleep", no_sleep)
    for name in ("load_credentials", "llm_ask", "LNClient", "evaluate_and_deduplicate"):
        monkeypatch.setattr(mod, name, lambda *a, **k: pytest.fail("catch-up must only read/enqueue"))
    monkeypatch.setattr(mod._provider_chain, "reset_failures",
                        lambda: pytest.fail("catch-up must not reset provider breakers"))
    monkeypatch.setattr(type(db), "prune_ws_events",
                        lambda *a, **k: pytest.fail("cleanup belongs to daily maintenance"))
    asyncio.run(mod.run_agent(object()))
    asyncio.run(mod.run_agent(object()))
    assert [r["msg_id"] for r in db.get_pending_source_messages()] == [1, 2]
    assert db.get_cursor("@source") == 2
    assert db._execute("SELECT COUNT(*) FROM evaluated_messages").fetchone()[0] == 0


def _fake_ln(monkeypatch, mod, articles):
    calls = {"auth": 0, "yaps": [], "markets": 0}
    class Client:
        user_id = 42
        def __init__(self, key): pass
        def authenticate(self): calls["auth"] += 1
        def get_recent_articles(self, per_page=20): return articles
        def get_yaps(self, article_id):
            calls["yaps"].append(article_id)
            return [{"id": 100 + article_id, "text": "Our comment", "author": {"id": 42}}]
    monkeypatch.setattr(mod, "load_credentials", lambda: (None, None, "test-key"))
    monkeypatch.setattr(mod, "LNClient", Client)
    return calls


def test_engagement_reconciles_without_telegram_and_preserves_events_arriving_during_pass(agent, monkeypatch):
    mod, db = agent
    articles = [{"id": 10, "headline": "One"}]
    _fake_ln(monkeypatch, mod, articles)
    for aid in (10, 11):
        db.add_ws_event("news.approved", aid, "slug", "News", "", "test", "{}")
    checked = []
    async def vote(ln, work_db, rows, since):
        checked.extend(rows)
        assert since is None
        work_db.add_ws_event("news.approved", 12, "slug", "During pass", "", "test", "{}")
        return 1, 0, {10}
    monkeypatch.setattr(mod, "vote_comment_pass", vote)
    monkeypatch.setattr(mod, "ENABLE_MARKET_MATCH", True)
    markets = []
    monkeypatch.setattr(mod, "run_market_match_phase", lambda *a: markets.append(True))
    asyncio.run(mod.run_engagement_pass())
    assert checked == articles and markets == [True]
    assert {r["news_id"] for r in db.get_unconsumed_ws_events("agent")} == {11, 12}


def test_reply_pass_checks_older_commented_articles_without_running_votes(agent, monkeypatch):
    mod, db = agent
    articles = [{"id": aid, "headline": f"Old article {aid}"} for aid in (10, 11)]
    calls = _fake_ln(monkeypatch, mod, articles)
    db.save_comment(10, "Our old comment")
    walked = []
    monkeypatch.setattr(mod, "walk_replies_and_respond", lambda *a: walked.append(a[4]))
    monkeypatch.setattr(mod, "vote_comment_pass", lambda *a: pytest.fail("reply scan must not vote"))
    async def no_sleep(*a): pass
    monkeypatch.setattr(mod.asyncio, "sleep", no_sleep)
    asyncio.run(mod.run_reply_pass())
    assert calls["yaps"] == [10] and walked == [10]


def test_daily_maintenance_prunes_terminal_state_but_keeps_retryable_jobs(agent):
    mod, db = agent
    old = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    for mid in (1, 2):
        db.add_pending_source_message("@source", mid, "message", old)
    queue = mod.NewsQueue(db.db_path)
    try:
        queue.ingest()
        rows = queue.ready("evaluate", 10)
        queue.finish_post(rows[0])
        with queue.conn:
            queue.conn.execute("UPDATE jev_news_jobs SET updated_at=0")
        db._execute("UPDATE pending_source_messages SET received_at=?", (old,))
        db._commit()
        mod.run_daily_maintenance()
        assert [r["msg_id"] for r in queue.ready("evaluate", 10)] == [2]
        assert queue.conn.execute("SELECT COUNT(*) FROM jev_news_jobs").fetchone()[0] == 1
    finally:
        queue.close()


def test_engagement_scheduler_serializes_passes_and_keeps_reply_timer_independent(agent, monkeypatch):
    mod, _ = agent
    monkeypatch.setattr(mod, "ENGAGEMENT_RECONCILE_INTERVAL", .08)
    monkeypatch.setattr(mod, "REPLY_CHECK_INTERVAL", .02)
    monkeypatch.setattr(mod, "MINI_PASS_MIN_INTERVAL", .01)
    monkeypatch.setattr(mod, "ENABLE_WS_MINI_PASS", True)
    calls = []
    active = 0
    async def job(name):
        nonlocal active
        active += 1
        assert active == 1
        calls.append(name)
        await asyncio.sleep(.005)
        active -= 1
    async def reconcile(): await job("reconcile")
    async def replies(): await job("replies")
    async def mini(): await job("mini")
    monkeypatch.setattr(mod, "run_engagement_pass", reconcile)
    monkeypatch.setattr(mod, "run_reply_pass", replies)
    monkeypatch.setattr(mod, "_run_guarded_mini_pass", mini)
    async def run():
        mod._ws_wake = asyncio.Event()
        worker = asyncio.create_task(mod._engagement_worker())
        try:
            await asyncio.sleep(.025)
            mod._ws_wake.set()
            await asyncio.sleep(.065)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
    asyncio.run(run())
    assert "reconcile" in calls and "mini" in calls
    assert calls.count("replies") >= 2


def test_scheduled_failure_is_logged_and_does_not_stop_later_jobs(agent, monkeypatch, caplog):
    mod, _ = agent
    monkeypatch.setattr(mod, "ENGAGEMENT_RECONCILE_INTERVAL", .05)
    monkeypatch.setattr(mod, "REPLY_CHECK_INTERVAL", .01)
    monkeypatch.setattr(mod, "ENABLE_WS_MINI_PASS", False)
    replies = []
    async def failed(): raise RuntimeError("synthetic API failure")
    async def reply(): replies.append(True)
    monkeypatch.setattr(mod, "run_engagement_pass", failed)
    monkeypatch.setattr(mod, "run_reply_pass", reply)
    async def run():
        worker = asyncio.create_task(mod._engagement_worker())
        try:
            await asyncio.sleep(.045)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
    asyncio.run(run())
    assert replies
    assert "engagement" in caplog.text and "RuntimeError" in caplog.text


@pytest.mark.parametrize("catchup_waits", [False, True])
def test_realtime_loop_runs_background_jobs_during_source_catchup_and_drains_on_stop(
        agent, monkeypatch, catchup_waits):
    mod, db = agent
    monkeypatch.setattr(mod, "ENABLE_WS_EVENTS", False)
    monkeypatch.setattr(mod, "ENABLE_SOURCE_PUSH", True)
    monkeypatch.setattr(mod, "ENABLE_WS_MINI_PASS", True)
    monkeypatch.setattr(mod, "MINI_PASS_MIN_INTERVAL", 0)
    legacy_calls = []
    async def legacy_mini(): legacy_calls.append(True)
    monkeypatch.setattr(mod, "_run_guarded_mini_pass", legacy_mini)
    mod._ws_wake.set()
    started, drained = set(), set()
    async def worker(name):
        started.add(name)
        try:
            await asyncio.Future()
        finally:
            drained.add(name)
    for name in ("_jev_evaluation_worker", "_jev_recovery_worker", "_jev_publication_worker",
                 "_engagement_worker", "_maintenance_worker"):
        async def stub(name=name): await worker(name)
        monkeypatch.setattr(mod, name, stub, raising=False)
    async def cycle(client):
        if catchup_waits:
            await worker("catchup")
        else:
            started.add("catchup")
            drained.add("catchup")
        return False
    monkeypatch.setattr(mod, "_run_guarded_cycle", cycle)
    class Client:
        def __init__(self):
            self._disconnected = asyncio.get_running_loop().create_future()
        def is_connected(self): return True
        @property
        def disconnected(self): return asyncio.shield(self._disconnected)
        async def disconnect(self):
            if not self._disconnected.done(): self._disconnected.set_result(None)
    async def build(): return Client()
    async def register(*a): return 96
    monkeypatch.setattr(mod, "_build_client", build)
    monkeypatch.setattr(mod, "register_source_handlers", register)
    async def run():
        task = asyncio.create_task(mod.run_loop())
        try:
            await asyncio.sleep(.03)
            assert {"catchup", "_engagement_worker", "_maintenance_worker"} <= started
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert started == drained
        assert not legacy_calls
    asyncio.run(run())


def test_maintenance_has_its_own_daily_interval(agent, monkeypatch):
    mod, _ = agent
    calls = []
    monkeypatch.setattr(mod, "MAINTENANCE_INTERVAL", .05)
    monkeypatch.setattr(mod, "run_daily_maintenance", lambda: calls.append(True))
    async def run():
        worker = asyncio.create_task(mod._maintenance_worker())
        try:
            await asyncio.sleep(.025)
            assert len(calls) == 1
            await asyncio.sleep(.05)
            assert len(calls) == 2
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
    asyncio.run(run())


def test_failed_engagement_does_not_discard_events(agent, monkeypatch):
    mod, db = agent
    _fake_ln(monkeypatch, mod, [{"id": 10, "headline": "One"}])
    db.add_ws_event("news.approved", 10, "slug", "News", "", "test", "{}")
    async def fail(*a, **k): raise RuntimeError("synthetic failure")
    monkeypatch.setattr(mod, "vote_comment_pass", fail)
    with pytest.raises(RuntimeError, match="synthetic failure"):
        asyncio.run(mod.run_engagement_pass())
    assert [r["news_id"] for r in db.get_unconsumed_ws_events("agent")] == [10]
