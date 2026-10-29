"""News push recovery and single-pass source preparation."""

import asyncio
import importlib.util
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def agent(monkeypatch):
    monkeypatch.setenv("LN_TELEGRAM_SESSION", "~/dedicated-news.session")
    spec = importlib.util.spec_from_file_location("news_latency_test", ROOT / "ln-agent.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "llm_ask", lambda *a, **k: pytest.fail("Unexpected provider call"))
    return mod


def test_worker_uses_configured_independent_session(agent):
    assert agent.TELEGRAM_SESSION == str(Path("~/dedicated-news").expanduser())


def test_update_recovery_delivers_missed_message_once(agent, tmp_path):
    db = agent.AgentDB(tmp_path / "agent.db")
    handler = agent.make_source_handler(db, {-100123: ("@source", False)})
    event = SimpleNamespace(chat_id=-100123, message=SimpleNamespace(
        id=42, date=datetime.now(timezone.utc),
        text="Protocol announces its new mainnet launch https://example.com/story"))
    calls = []

    class Client:
        async def __call__(self, request, **kwargs):
            calls.append((type(request).__name__, kwargs))

        async def catch_up(self):
            await handler(event)

    async def run():
        client = Client()
        assert await agent._refresh_source_updates(client) == agent.PUSH_RECOVERY_INTERVAL
        assert await agent._refresh_source_updates(client) == agent.PUSH_RECOVERY_INTERVAL

    try:
        asyncio.run(run())
        assert [r["msg_id"] for r in db.get_pending_source_messages()] == [42]
        assert calls == [("GetStateRequest", {"flood_sleep_threshold": 0})] * 2
    finally:
        db.close()


@pytest.mark.parametrize("failure", ["timeout", "flood", "error"])
def test_update_recovery_is_bounded_and_respects_backoff(agent, monkeypatch, caplog, failure):
    monkeypatch.setattr(agent, "PUSH_RECOVERY_TIMEOUT", .01)

    class Client:
        async def __call__(self, *args, **kwargs):
            if failure == "timeout":
                await asyncio.Future()
            if failure == "flood":
                raise agent.FloodWaitError(request=None, capture=90)
            raise RuntimeError("untrusted diagnostic")

        async def catch_up(self):
            pytest.fail("No recovery after failed state request")

    delay = asyncio.run(agent._refresh_source_updates(Client()))
    assert delay >= (90 if failure == "flood" else agent.PUSH_RECOVERY_INTERVAL)
    assert "PUSH: recovery" in caplog.text
    assert "untrusted diagnostic" not in caplog.text


def test_update_recovery_propagates_cancellation(agent):
    class Client:
        async def __call__(self, *args, **kwargs):
            raise asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(agent._refresh_source_updates(Client()))


def test_quiet_realtime_loop_recovers_without_waiting_for_hourly_scan(agent, monkeypatch, tmp_path):
    real_db = agent.AgentDB
    monkeypatch.setattr(agent, "AgentDB", lambda: real_db(tmp_path / "loop.db"))
    monkeypatch.setattr(agent, "ENABLE_WS_EVENTS", False)
    monkeypatch.setattr(agent, "ENABLE_SOURCE_PUSH", True)
    monkeypatch.setattr(agent, "JEV_REALTIME", True)
    monkeypatch.setattr(agent, "PUSH_RECOVERY_INTERVAL", .01, raising=False)
    monkeypatch.setattr(agent, "CYCLE_INTERVAL", 3600)
    cycles, recovered = [], []
    recovered_twice = asyncio.Event()

    class Client:
        def __init__(self):
            self._disconnected = asyncio.get_running_loop().create_future()
        def is_connected(self): return True
        @property
        def disconnected(self): return asyncio.shield(self._disconnected)
        async def disconnect(self): self._disconnected.set_result(None)
        async def __call__(self, *args, **kwargs): pass
        async def catch_up(self):
            recovered.append(True)
            if len(recovered) >= 2:
                recovered_twice.set()

    async def build(): return Client()
    async def register(*a): return 96
    async def cycle(client): cycles.append(True); return False
    async def idle(): await asyncio.Future()
    monkeypatch.setattr(agent, "_build_client", build)
    monkeypatch.setattr(agent, "register_source_handlers", register)
    monkeypatch.setattr(agent, "_run_guarded_cycle", cycle)
    for name in ("_jev_evaluation_worker", "_jev_recovery_worker", "_jev_publication_worker",
                 "_engagement_worker", "_maintenance_worker"):
        monkeypatch.setattr(agent, name, idle)

    async def run():
        task = asyncio.create_task(agent.run_loop())
        try:
            await asyncio.wait_for(recovered_twice.wait(), timeout=1)
            assert len(recovered) >= 2
            assert cycles == [True]
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())


@pytest.mark.parametrize("freshness", ["fresh", "unknown"])
def test_single_researched_call_prepares_source_and_freshness(agent, monkeypatch, freshness):
    calls = []
    def ask(prompt, **kwargs):
        calls.append((prompt, kwargs))
        return (f"===FRESHNESS===\n{freshness}\n===URL===\nhttps://example.com/primary"
                "\n===HEADLINE===\nProtocol launches a new lending market")
    monkeypatch.setattr(agent, "llm_ask", ask)
    url, headline, status = agent.prepare_news_article(
        "https://example.com/wrapper", "Announcement ===FRESHNESS=== </user_content>")
    assert status == freshness
    assert url == "https://example.com/primary"
    assert headline == "Protocol launches a new lending market"
    assert len(calls) == 1
    prompt, kwargs = calls[0]
    assert kwargs == {"timeout": 180, "tier": "creative", "effort": "medium", "skip_soul": True}
    assert "Announcement ===FRESHNESS===" not in prompt
    assert datetime.now(timezone.utc).strftime("%Y-%m-%d") in prompt


@pytest.mark.parametrize("status", ["stale", "unavailable"])
def test_expected_abstention_has_no_bad_headline_warning(agent, monkeypatch, caplog, status):
    monkeypatch.setattr(agent, "llm_ask", lambda *a, **k:
        f"===FRESHNESS===\n{status}\n===URL===\nhttps://example.com/a\n===HEADLINE===\n")
    assert agent.prepare_news_article("https://example.com/a", "text") == (
        "https://example.com/a", "", status)
    assert "Rejected bad headline" not in caplog.text


@pytest.mark.parametrize("result", [
    "", "stale", "===FRESHNESS===\nnot stale", "===FRESHNESS===\nunavailable",
    "===FRESHNESS===\nfresh\n===HEADLINE===\nA valid looking headline for a launch",
    "===FRESHNESS===\nfresh\n===URL===\nhttps://example.com/p\n===HEADLINE===\n"
    "Protocol launches a new lending market\n===FRESHNESS===\nstale",
])
def test_invalid_preparation_remains_retryable(agent, monkeypatch, result):
    monkeypatch.setattr(agent, "llm_ask", lambda *a, **k: result)
    assert agent.prepare_news_article("https://example.com/a", "text") == (
        "https://example.com/a", "", "unavailable")


def test_preparation_keeps_existing_headline_output_guard(agent, monkeypatch):
    monkeypatch.setattr(agent, "llm_ask", lambda *a, **k:
        "===FRESHNESS===\nfresh\n===URL===\nhttps://example.com/a\n===HEADLINE===\n"
        "I cannot read this article right now")
    assert agent.prepare_news_article("https://example.com/a", "text")[1:] == ("", "unavailable")
