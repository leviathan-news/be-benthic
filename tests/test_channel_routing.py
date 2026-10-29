"""Tests for news channel routing (heat signal, post_to_channel, destination-scoped posting pass)."""

import html
import importlib.util
import json
import logging
import os
import sqlite3
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


def _load_agent_module():
    """Import ln-agent.py under a Python-safe module name (hyphen in filename)."""
    if "ln_agent_route_test" in sys.modules:
        return sys.modules["ln_agent_route_test"]
    spec = importlib.util.spec_from_file_location(
        "ln_agent_route_test", ROOT / "ln-agent.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ln_agent_route_test"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def agent(tmp_path):
    mod = _load_agent_module()
    db = mod.AgentDB(db_path=tmp_path / "test.db")
    yield mod, db
    db.close()


# ── Heat parsing ─────────────────────────────────────────────────────────

def test_parse_heat_valid_and_clamped():
    mod = _load_agent_module()
    assert mod._parse_heat(7) == 7
    assert mod._parse_heat("8") == 8      # models emit strings sometimes
    assert mod._parse_heat(15) == 10      # clamp high
    assert mod._parse_heat(-3) == 1       # clamp low
    assert mod._parse_heat(0) == 1        # explicit 0 is a valid int → clamps to 1


def test_parse_heat_missing_or_malformed_is_zero():
    mod = _load_agent_module()
    assert mod._parse_heat(None) == 0
    assert mod._parse_heat("hot") == 0
    assert mod._parse_heat({}) == 0
    # json.loads turns an overlarge literal (1e400) into inf, and int(inf)
    # raises OverflowError — it must degrade like any other malformed value
    # instead of escaping evaluate_and_deduplicate and losing the batch.
    assert mod._parse_heat(json.loads('{"heat": 1e400}')["heat"]) == 0


def test_evaluated_messages_has_heat_column(agent):
    mod, db = agent
    cols = [r[1] for r in db._execute(
        "PRAGMA table_info(evaluated_messages)").fetchall()]
    assert "heat" in cols


def test_save_evaluation_stores_heat(agent):
    mod, db = agent
    db.save_evaluation("@chan", 1, "text", url="https://a.com/x",
                       is_newsworthy=True, reason="r", headline_hint="h", heat=9)
    row = db._execute(
        "SELECT heat FROM evaluated_messages WHERE channel='@chan' AND msg_id=1"
    ).fetchone()
    assert row["heat"] == 9


def test_migration_adds_heat_to_legacy_evaluated_messages(tmp_path):
    """Production agent.db predates heat — the ALTER branch must run and keep rows."""
    mod = _load_agent_module()
    legacy = tmp_path / "legacy.db"
    conn = sqlite3.connect(legacy)
    conn.execute("""CREATE TABLE evaluated_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        channel TEXT NOT NULL,
        msg_id INTEGER NOT NULL,
        text TEXT,
        url TEXT,
        is_newsworthy INTEGER NOT NULL DEFAULT 0,
        reason TEXT,
        headline_hint TEXT,
        evaluated_at TEXT NOT NULL,
        UNIQUE(channel, msg_id)
    )""")
    conn.execute("INSERT INTO evaluated_messages (channel, msg_id, text, is_newsworthy, "
                 "evaluated_at) VALUES ('@old', 5, 'legacy', 1, ?)",
                 (datetime.now(timezone.utc).isoformat(),))
    conn.commit()
    conn.close()

    db = mod.AgentDB(db_path=legacy)
    try:
        row = db._execute(
            "SELECT heat FROM evaluated_messages WHERE msg_id=5").fetchone()
        assert row["heat"] is None  # pre-heat rows stay unscored, not zeroed
    finally:
        db.close()


def test_evaluate_and_deduplicate_threads_heat_to_results_and_db(agent, monkeypatch):
    """Heat reaches both the returned items and the stored evaluation; a model
    that omits the field degrades to 0 rather than an invented score."""
    mod, db = agent
    messages = [
        {"id": 1, "channel": "@a", "text": "Big protocol exploit https://coindesk.com/a"},
        {"id": 2, "channel": "@b", "text": "Seed round closed https://theblock.co/b"},
    ]
    # Eval calls llm_ask directly (classification tier) since the 2026-08-08
    # quota fix — patching claude_ask here would let a REAL provider call leak.
    monkeypatch.setattr(mod, "llm_ask", lambda *a, **kw: json.dumps([
        {"msg_id": 1, "url": "https://coindesk.com/a", "headline_hint": "h1",
         "reason": "r1", "heat": 9},
        {"msg_id": 2, "url": "https://theblock.co/b", "headline_hint": "h2",
         "reason": "r2"},  # heat omitted
    ]))

    results = mod.evaluate_and_deduplicate(messages, db)

    assert {r["id"]: r["heat"] for r in results} == {1: 9, 2: 0}
    stored = {r["msg_id"]: r["heat"] for r in db._execute(
        "SELECT msg_id, heat FROM evaluated_messages").fetchall()}
    assert stored == {1: 9, 2: 0}


def test_eval_prompts_document_heat():
    for name in ("evaluate_and_deduplicate", "evaluate_and_deduplicate_retry"):
        text = (ROOT / "prompts" / "agent" / f"{name}.md").read_text()
        assert '"heat"' in text, f"{name}.md missing heat field in JSON schema"
    main = (ROOT / "prompts" / "agent" / "evaluate_and_deduplicate.md").read_text()
    assert "8-10" in main and "grade-inflate" in main.replace("grade inflate", "grade-inflate")


# ── posted_articles destination columns ──────────────────────────────────

def test_posted_articles_new_columns(agent):
    mod, db = agent
    cols = [r[1] for r in db._execute(
        "PRAGMA table_info(posted_articles)").fetchall()]
    for col in ("destination", "tg_message_id", "heat"):
        assert col in cols


def test_save_posted_records_destination(agent):
    mod, db = agent
    db.save_posted(url="https://a.com/x", headline="H", story_hint="hint",
                   ln_article_id=None, source_channel="@c",
                   destination="channel", tg_message_id=42, heat=6)
    row = db._execute(
        "SELECT destination, tg_message_id, heat, ln_article_id "
        "FROM posted_articles WHERE url='https://a.com/x'").fetchone()
    assert (row["destination"], row["tg_message_id"], row["heat"]) == ("channel", 42, 6)
    assert row["ln_article_id"] is None


def test_save_posted_legacy_call_still_works(agent):
    """Marker rows ([blocked source] etc.) keep calling with the old signature."""
    mod, db = agent
    db.save_posted(url="https://b.com/y", headline="[blocked source]",
                   story_hint="h", source_channel="@c")
    row = db._execute(
        "SELECT destination FROM posted_articles WHERE url='https://b.com/y'").fetchone()
    assert row["destination"] is None


def test_posted_articles_legacy_db_migrates(tmp_path):
    """A pre-existing DB without the new columns gains them on AgentDB init."""
    mod = _load_agent_module()
    legacy = tmp_path / "legacy.db"
    conn = sqlite3.connect(legacy)
    conn.execute("""CREATE TABLE posted_articles (
        id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT NOT NULL, headline TEXT,
        story_hint TEXT, ln_article_id INTEGER, source_channel TEXT,
        posted_at TEXT NOT NULL)""")
    conn.commit()
    conn.close()
    db = mod.AgentDB(db_path=legacy)
    try:
        cols = [r[1] for r in db._execute(
            "PRAGMA table_info(posted_articles)").fetchall()]
        assert "destination" in cols and "tg_message_id" in cols and "heat" in cols
    finally:
        db.close()


# ── post_to_channel ──────────────────────────────────────────────────────

class _FakeResp:
    def __init__(self, status_code, body=None):
        self.status_code = status_code
        self._body = body if body is not None else {}

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


@pytest.fixture
def channel_env(monkeypatch):
    """Token + no real sleeps. Hard-cap sleep calls (runaway guard)."""
    mod = _load_agent_module()
    monkeypatch.setattr(mod, "_channel_bot_token", "TESTTOKEN", raising=False)
    monkeypatch.setattr(mod, "_channel_token_failed", False, raising=False)
    calls = {"sleep": 0}

    def capped_sleep(_s):
        calls["sleep"] += 1
        assert calls["sleep"] < 20, "runaway sleep loop"
    monkeypatch.setattr(mod.time, "sleep", capped_sleep)
    return mod, calls


def test_post_to_channel_success(channel_env, monkeypatch):
    mod, _ = channel_env
    seen = {}

    def fake_post(api_url, json=None, timeout=None):
        seen["url"] = api_url
        seen["payload"] = json
        return _FakeResp(200, {"ok": True, "result": {"message_id": 77}})
    monkeypatch.setattr(mod.requests, "post", fake_post)
    sent, msg_id = mod.post_to_channel("Headline.", "https://a.com/x")
    assert sent is True and msg_id == 77
    assert seen["payload"]["chat_id"] == mod.BENTHIC_NEWS_CHANNEL
    assert seen["payload"]["text"] == "Headline.\n\nhttps://a.com/x"
    assert "botTESTTOKEN/sendMessage" in seen["url"]


def test_post_to_channel_200_unparseable_body_is_still_sent(channel_env, monkeypatch):
    mod, _ = channel_env
    monkeypatch.setattr(mod.requests, "post",
                        lambda *a, **k: _FakeResp(200, ValueError("bad json")))
    sent, msg_id = mod.post_to_channel("H", "https://a.com/x")
    assert sent is True and msg_id is None


def test_post_to_channel_retries_5xx_then_fails(channel_env, monkeypatch):
    mod, _ = channel_env
    attempts = []
    monkeypatch.setattr(mod.requests, "post",
                        lambda *a, **k: attempts.append(1) or _FakeResp(502))
    sent, msg_id = mod.post_to_channel("H", "https://a.com/x")
    assert sent is False and msg_id is None
    assert len(attempts) == 3  # initial + 2 retries


def test_post_to_channel_429_honors_retry_after_then_succeeds(channel_env, monkeypatch):
    """429 is the one retryable 4xx: Telegram flood-controls channels at ~20
    msg/min and this feature routes more volume to the channel than to LN."""
    mod, _ = channel_env
    waits = []

    def rec(s):
        waits.append(s)
        assert len(waits) < 20, "runaway sleep loop"
    monkeypatch.setattr(mod.time, "sleep", rec)
    responses = [_FakeResp(429, {"parameters": {"retry_after": 7}}),
                 _FakeResp(200, {"ok": True, "result": {"message_id": 5}})]
    seen = {}

    def fake_post(api_url, json=None, timeout=None):
        seen["timeout"] = timeout
        return responses.pop(0)
    monkeypatch.setattr(mod.requests, "post", fake_post)
    assert mod.post_to_channel("H", "https://a.com/x") == (True, 5)
    assert 7 in waits              # server's retry_after, not the 2s default
    assert seen["timeout"] == 10   # per-attempt cap keeps the worst case bounded


def test_post_to_channel_429_retry_after_is_capped(channel_env, monkeypatch):
    """An unbounded server-supplied sleep would stall a Phase 3 worker for the
    rest of the cycle, so retry_after is honored only up to the cap."""
    mod, _ = channel_env
    waits = []

    def rec(s):
        waits.append(s)
        assert len(waits) < 20, "runaway sleep loop"
    monkeypatch.setattr(mod.time, "sleep", rec)
    monkeypatch.setattr(mod.requests, "post",
                        lambda *a, **k: _FakeResp(429, {"parameters": {"retry_after": 600}}))
    assert mod.post_to_channel("H", "https://a.com/x") == (False, None)
    assert 600 not in waits
    assert max(waits) == mod._CHANNEL_RETRY_AFTER_CAP


def test_post_to_channel_429_without_retry_after_uses_default_backoff(channel_env, monkeypatch):
    mod, _ = channel_env
    waits = []

    def rec(s):
        waits.append(s)
        assert len(waits) < 20, "runaway sleep loop"
    monkeypatch.setattr(mod.time, "sleep", rec)
    monkeypatch.setattr(mod.requests, "post",
                        lambda *a, **k: _FakeResp(429, ValueError("no body")))
    assert mod.post_to_channel("H", "https://a.com/x") == (False, None)
    assert waits == [2, 4]


def test_post_to_channel_429_on_final_attempt_gives_up(channel_env, monkeypatch):
    mod, _ = channel_env
    attempts = []
    monkeypatch.setattr(
        mod.requests, "post",
        lambda *a, **k: attempts.append(1) or _FakeResp(429, {"parameters": {"retry_after": 1}}))
    assert mod.post_to_channel("H", "https://a.com/x") == (False, None)
    assert len(attempts) == 3  # retried within the budget, then final


def test_post_to_channel_lock_not_held_during_backoff(channel_env, monkeypatch):
    """Phase 3 posts articles from parallel threads. Holding the send lock across
    the whole retry budget would serialize every worker behind one flooding
    article and eat the cycle deadline. The 0.5s courtesy spacing stays under
    the lock (it is what the lock is for); the waits must not."""
    mod, _ = channel_env
    observed = []

    def rec(s):
        observed.append((s, mod._channel_send_lock.locked()))
        assert len(observed) < 20, "runaway sleep loop"
    monkeypatch.setattr(mod.time, "sleep", rec)
    responses = [_FakeResp(502), _FakeResp(200, {"ok": True, "result": {"message_id": 9}})]
    monkeypatch.setattr(mod.requests, "post", lambda *a, **k: responses.pop(0))
    assert mod.post_to_channel("H", "https://a.com/x") == (True, 9)
    assert (2, False) in observed    # backoff wait — lock released
    assert (0.5, True) in observed   # courtesy spacing — lock held


def test_post_to_channel_403_no_retry(channel_env, monkeypatch):
    mod, _ = channel_env
    attempts = []
    monkeypatch.setattr(mod.requests, "post",
                        lambda *a, **k: attempts.append(1) or _FakeResp(403))
    sent, _ = mod.post_to_channel("H", "https://a.com/x")
    assert sent is False
    assert len(attempts) == 1  # 4xx is not retryable


def test_post_to_channel_network_error_never_raises(channel_env, monkeypatch):
    mod, _ = channel_env

    def boom(*a, **k):
        raise OSError("connection reset")
    monkeypatch.setattr(mod.requests, "post", boom)
    sent, msg_id = mod.post_to_channel("H", "https://a.com/x")
    assert (sent, msg_id) == (False, None)


def test_post_to_channel_network_error_does_not_log_token(channel_env, monkeypatch, caplog):
    """requests embeds the request URL in connection errors — and the URL carries
    the bot token. agent.log is rotated on disk and readable by operator Bash
    diagnostics that echo output into chat, so the token must be redacted."""
    mod, _ = channel_env

    def boom(*a, **k):
        raise OSError("Max retries exceeded with url: /botTESTTOKEN/sendMessage")
    monkeypatch.setattr(mod.requests, "post", boom)
    with caplog.at_level(logging.WARNING, logger="ln-agent"):
        assert mod.post_to_channel("H", "https://a.com/x") == (False, None)
    assert "Channel post attempt" in caplog.text  # the failure was reported
    assert "TESTTOKEN" not in caplog.text


def test_post_to_channel_without_token_disabled(channel_env, monkeypatch):
    mod, _ = channel_env
    monkeypatch.setattr(mod, "_channel_bot_token", None)
    monkeypatch.setattr(mod, "_channel_token_failed", True)
    called = []
    monkeypatch.setattr(mod.requests, "post", lambda *a, **k: called.append(1))
    assert mod.post_to_channel("H", "https://a.com/x") == (False, None)
    assert not called


def test_get_channel_bot_token_prefers_env_and_caches(monkeypatch):
    mod = _load_agent_module()
    monkeypatch.setattr(mod, "_channel_bot_token", None, raising=False)
    monkeypatch.setattr(mod, "_channel_token_failed", False, raising=False)
    monkeypatch.setenv("BOT_TOKEN", "ENVTOKEN")
    assert mod._get_channel_bot_token() == "ENVTOKEN"
    monkeypatch.delenv("BOT_TOKEN")
    assert mod._get_channel_bot_token() == "ENVTOKEN"  # cached, not re-read


@pytest.mark.parametrize("where,exc", [
    ("read_text", OSError("permission denied")),
    # A non-UTF-8 token file raises UnicodeDecodeError (a ValueError), and
    # expanduser() raises RuntimeError when HOME cannot be resolved — neither
    # is an OSError, and either would escape the never-raises contract.
    ("read_text", UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")),
    ("expanduser", RuntimeError("Could not determine home directory")),
])
def test_get_channel_bot_token_latches_disabled_on_unreadable_file(monkeypatch, where, exc):
    """An unreachable/corrupt token file must disable posting once — not raise
    through post_to_channel on every article in the parallel posting path."""
    mod = _load_agent_module()
    monkeypatch.setattr(mod, "_channel_bot_token", None, raising=False)
    monkeypatch.setattr(mod, "_channel_token_failed", False, raising=False)
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    calls = []

    class _BadPath:
        def __init__(self, *a):
            pass

        def expanduser(self):
            calls.append("expanduser")
            if where == "expanduser":
                raise exc
            return self

        def read_text(self):
            calls.append("read_text")
            raise exc

    monkeypatch.setattr(mod, "Path", _BadPath)
    monkeypatch.setattr(mod.requests, "post",
                        lambda *a, **k: pytest.fail("must not send without a token"))
    assert mod.post_to_channel("H", "https://a.com/x") == (False, None)
    assert mod.post_to_channel("H", "https://a.com/x") == (False, None)
    assert calls.count(where) == 1  # CRITICAL once, then latched off


# ── LN link in channel posts (spec 2026-08-04) ───────────────────────────

_LN_URL = "https://leviathannews.xyz/287122/gotbit-agrees-to-settle"
_UTM = "utm_source=benthic_news&utm_medium=telegram&utm_campaign=news"


def test_channel_payloads_without_link_is_plain_single():
    mod = _load_agent_module()
    payloads = mod._channel_payloads("Headline.", "https://a.com/x", None)
    assert payloads == [{"chat_id": mod.BENTHIC_NEWS_CHANNEL,
                         "text": "Headline.\n\nhttps://a.com/x",
                         "link_preview_options": {"is_disabled": True}}]


def test_channel_payloads_with_link_rich_then_plain():
    mod = _load_agent_module()
    rich, plain = mod._channel_payloads("Headline.", "https://a.com/x", _LN_URL)
    assert rich["parse_mode"] == "HTML"
    # Story link rides a "Source" anchor (operator decision 2026-08-12: no raw
    # redirect URL in the message body), both anchors on one line.
    assert rich["text"] == (
        'Headline.\n\n<a href="https://a.com/x">Source</a> '
        f'⚓ <a href="{_LN_URL}?{_UTM.replace("&", "&amp;")}">Discuss on Leviathan</a>')
    assert plain == {"chat_id": mod.BENTHIC_NEWS_CHANNEL,
                     "text": "Headline.\n\nhttps://a.com/x",
                     "link_preview_options": {"is_disabled": True}}


def test_channel_payloads_all_disable_link_previews():
    mod = _load_agent_module()
    no_link = mod._channel_payloads("H.", "https://a.com/x", None)
    with_link = mod._channel_payloads("H.", "https://a.com/x", _LN_URL)
    for p in no_link + with_link:
        assert p["link_preview_options"] == {"is_disabled": True}


def test_channel_payloads_escape_html_and_append_utm_to_existing_query():
    mod = _load_agent_module()
    rich, _ = mod._channel_payloads('M&A "deal" <soon>', "https://a.com/?q=1&r=2",
                                    "https://leviathannews.xyz/1/s?x=1")
    assert "M&amp;A &quot;deal&quot; &lt;soon&gt;" in rich["text"]
    assert '<a href="https://a.com/?q=1&amp;r=2">Source</a>' in rich["text"]
    assert f'href="https://leviathannews.xyz/1/s?x=1&amp;{_UTM.replace("&", "&amp;")}"' in rich["text"]


def test_post_to_channel_sends_rich_payload_when_ln_url_given(channel_env, monkeypatch):
    mod, _ = channel_env
    seen = []

    def fake_post(u, json=None, timeout=None):
        seen.append(json)
        return _FakeResp(200, {"ok": True, "result": {"message_id": 9}})
    monkeypatch.setattr(mod.requests, "post", fake_post)
    sent, msg_id = mod.post_to_channel("H.", "https://a.com/x", ln_url=_LN_URL)
    assert sent is True and msg_id == 9
    assert len(seen) == 1 and seen[0]["parse_mode"] == "HTML"
    assert "Discuss on Leviathan" in seen[0]["text"]


def test_post_to_channel_400_on_rich_falls_back_to_plain(channel_env, monkeypatch):
    mod, _ = channel_env
    seen = []

    def fake_post(u, json=None, timeout=None):
        seen.append(json)
        if "parse_mode" in json:
            return _FakeResp(400, {"ok": False, "description": "can't parse entities"})
        return _FakeResp(200, {"ok": True, "result": {"message_id": 11}})
    monkeypatch.setattr(mod.requests, "post", fake_post)
    sent, msg_id = mod.post_to_channel("H.", "https://a.com/x", ln_url=_LN_URL)
    assert sent is True and msg_id == 11
    assert [("parse_mode" in p) for p in seen] == [True, False]


def test_post_to_channel_400_on_plain_stays_final(channel_env, monkeypatch):
    mod, _ = channel_env
    calls = {"n": 0}

    def fake_post(u, json=None, timeout=None):
        calls["n"] += 1
        return _FakeResp(400, {"ok": False})
    monkeypatch.setattr(mod.requests, "post", fake_post)
    sent, msg_id = mod.post_to_channel("H.", "https://a.com/x")
    assert (sent, msg_id) == (False, None)
    assert calls["n"] == 1  # legacy behavior: non-429 4xx is final on plain


def test_ln_submit_min_heat_default_and_type():
    mod = _load_agent_module()
    assert isinstance(mod.LN_SUBMIT_MIN_HEAT, int)
    assert 1 <= mod.LN_SUBMIT_MIN_HEAT <= 10


@pytest.mark.parametrize("raw,expected", [("99", 10), ("0", 1), ("nonsense", 8)])
def test_ln_submit_min_heat_clamps_operator_values(raw, expected):
    """The knob is parsed at import, so the clamp can only be exercised in a
    fresh interpreter. An out-of-range value must clamp (and warn) rather than
    silently routing everything — or nothing — to Leviathan; a malformed value
    falls back to the _env_int default."""
    out = subprocess.run(
        [sys.executable, "-c",
         "import importlib.util, sys\n"
         "spec = importlib.util.spec_from_file_location('m', 'ln-agent.py')\n"
         "m = importlib.util.module_from_spec(spec)\n"
         "sys.modules['m'] = m\n"
         "spec.loader.exec_module(m)\n"
         "print('HEAT=', m.LN_SUBMIT_MIN_HEAT)\n"],
        cwd=ROOT, env={**os.environ, "LN_SUBMIT_MIN_HEAT": raw},
        capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert f"HEAT= {expected}" in out.stdout
    if raw.isdigit():  # malformed values warn from _env_int, not the clamp
        assert ("clamped to" in out.stdout) is (int(raw) != expected)


# ── Destination-scoped posting pass ──────────────────────────────────────

class _FakeLN:
    def __init__(self, submit_ok=True, canonical=None, redirect=None):
        self.submit_ok = submit_ok
        self.canonical = canonical
        self.redirect = redirect
        self.submitted = []
        self.wire_flags = []
        self.votes = []

    def submit_article(self, url, headline, market_id=None, wire=False):
        self.submitted.append((url, headline, market_id))
        self.wire_flags.append(wire)
        if not self.submit_ok:
            return None
        # Mirrors the real API shape: article data nested under "news";
        # "url" is the skip-landing tracked redirect format_news_data emits.
        return {"article_id": 555,
                "news": {"canonical_url": self.canonical, "url": self.redirect}}

    def vote(self, art_id, weight, label=""):
        self.votes.append((art_id, weight))

    def get_open_markets(self):
        return []


def _item(url="https://src.com/story", heat=5, channel="@chan", hint="Aave exploit fund"):
    return {"channel": channel, "id": 1, "text": "story text", "date": "2026-08-02T10:00:00+00:00",
            "is_group": False, "url": url, "headline_hint": hint, "reason": "r", "heat": heat}


@pytest.fixture
def pipeline(agent, monkeypatch):
    """Posting pass with every external dependency stubbed. Returns knobs."""
    mod, db = agent
    state = {"channel_result": (True, 42), "prov": "fallback", "hq_llm": "not_duplicate",
             "hq_fetch": [], "hq_fetch_calls": 0, "prov_calls": 0, "hq_llm_calls": 0,
             "channel_calls": [], "ln_links": [], "story_links": [], "sleeps": []}
    monkeypatch.setattr(mod, "check_article_freshness", lambda u, t: True)
    monkeypatch.setattr(mod, "resolve_craft_headline",
                        lambda u, t: (u, "Crafted headline about the story."))
    def prepare(url, text):
        if not mod.check_article_freshness(url, text):
            return url, "", "stale"
        return (*mod.resolve_craft_headline(url, text), "fresh")
    monkeypatch.setattr(mod, "prepare_news_article", prepare, raising=False)

    def fake_prov(url, hint, recent_hours=168):
        state["prov_calls"] += 1
        return state["prov"]
    monkeypatch.setattr(mod, "_provenance_dedup_check", fake_prov)

    def fake_llm(prompt, **kw):
        state["hq_llm_calls"] += 1
        return state["hq_llm"]
    monkeypatch.setattr(mod, "llm_ask", fake_llm)

    def fake_hq_fetch(limit=300, hours=168):
        state["hq_fetch_calls"] += 1
        return state["hq_fetch"]
    monkeypatch.setattr(mod, "fetch_bot_hq_recent_headlines", fake_hq_fetch)

    def fake_channel(headline, url, ln_url=None, ln_story_url=None):
        state["channel_calls"].append((headline, url))
        state["ln_links"].append(ln_url)
        state["story_links"].append(ln_story_url)
        return state["channel_result"]
    monkeypatch.setattr(mod, "post_to_channel", fake_channel)

    # The HQ-fetch retry backoff awaits 2s then 4s. Suite rule: no real sleeps,
    # and a hard cap so a future retry-loop regression fails instead of hanging.
    async def no_sleep(seconds):
        state["sleeps"].append(seconds)
        assert len(state["sleeps"]) < 20, "runaway sleep loop"
    monkeypatch.setattr(mod.asyncio, "sleep", no_sleep)

    monkeypatch.setattr(mod, "ENABLE_CHANNEL_POSTS", True)
    monkeypatch.setattr(mod, "LN_SUBMIT_MIN_HEAT", 8)
    monkeypatch.setattr(mod, "ENABLE_MARKET_MATCH", False)
    # Pre-wire routing pins below; the wire tests flip this on explicitly.
    # The white-label CODE default is off (see test_routing_defaults_*).
    monkeypatch.setattr(mod, "ENABLE_WIRE_SUBMISSIONS", False)
    return mod, db, state


@pytest.mark.parametrize("status", ["fresh", "unknown", "stale", "unavailable"])
def test_jev_publication_uses_one_preparation_and_keeps_failures_retryable(pipeline, monkeypatch, status):
    mod, db, state = pipeline
    monkeypatch.setattr(mod.jev, "enabled", lambda task: task == "news")
    monkeypatch.setattr(mod, "check_article_freshness",
                        lambda *a: pytest.fail("Separate freshness call must not run"))
    monkeypatch.setattr(mod, "resolve_craft_headline",
                        lambda *a: pytest.fail("Separate headline call must not run"))
    calls = []
    def prepare(url, text):
        calls.append(url)
        return url, "Protocol launches a new lending market", status
    monkeypatch.setattr(mod, "prepare_news_article", prepare)
    item = _item()
    mod._process_article(item, db, _FakeLN(), [], True, 168, [])
    assert calls == [item["url"]]
    if status in {"fresh", "unknown"}:
        assert len(state["channel_calls"]) == 1
    else:
        assert state["channel_calls"] == []
    assert db.was_url_posted(item["url"]) == (status != "unavailable")


def _run_pass(mod, relevant, db, ln):
    import asyncio
    return asyncio.run(mod.news_post_pass(relevant, db, ln))


def test_destination_constants_are_the_stored_values():
    """One vocabulary for the implementation; the routing tests below assert the
    literals that actually land in posted_articles.destination."""
    mod = _load_agent_module()
    assert (mod.DEST_CHANNEL, mod.DEST_LN, mod.DEST_BOTH) == ("channel", "ln", "both")


def test_cold_story_channel_only_no_ln_checks(pipeline):
    mod, db, state = pipeline
    ln = _FakeLN()
    posted = _run_pass(mod, [_item(heat=5)], db, ln)
    assert posted == 1
    assert state["channel_calls"] and not ln.submitted
    # LN-ecosystem dedup never ran for a cold story
    assert state["prov_calls"] == 0 and state["hq_llm_calls"] == 0
    assert state["hq_fetch_calls"] == 0  # no hot candidate → no HQ fetch
    row = db._execute("SELECT destination, tg_message_id, heat FROM posted_articles").fetchone()
    assert (row["destination"], row["tg_message_id"], row["heat"]) == ("channel", 42, 5)


def test_hot_story_double_posts(pipeline):
    mod, db, state = pipeline
    ln = _FakeLN()
    posted = _run_pass(mod, [_item(heat=9)], db, ln)
    assert posted == 1
    assert state["channel_calls"] and ln.submitted
    assert ln.votes == [(555, 1)]  # auto-upvote preserved
    row = db._execute("SELECT destination, ln_article_id FROM posted_articles").fetchone()
    assert (row["destination"], row["ln_article_id"]) == ("both", 555)
    assert state["hq_fetch_calls"] == 1


def test_threshold_boundary(pipeline):
    mod, db, state = pipeline
    ln = _FakeLN()
    _run_pass(mod, [_item(url="https://a.com/1", heat=7, hint="Sol upgrade ship")], db, ln)
    assert not ln.submitted            # 7 < 8
    _run_pass(mod, [_item(url="https://b.com/2", heat=8, hint="BTC records etf flows")], db, ln)
    assert ln.submitted                # 8 >= 8


def test_hot_story_ln_dup_demotes_to_channel(pipeline):
    mod, db, state = pipeline
    state["prov"] = "reject"
    ln = _FakeLN()
    posted = _run_pass(mod, [_item(heat=9)], db, ln)
    assert posted == 1
    assert not ln.submitted            # LN leg blocked by dup
    assert state["channel_calls"]      # …but the channel still gets it
    row = db._execute("SELECT destination, headline FROM posted_articles").fetchone()
    assert row["destination"] == "channel"
    assert not row["headline"].startswith("[")  # real post, not a marker row


def test_hot_story_hq_llm_dup_demotes(pipeline):
    mod, db, state = pipeline
    state["prov"] = "fallback"
    state["hq_llm"] = "duplicate"
    state["hq_fetch"] = ["Existing HQ headline about Aave"]
    ln = _FakeLN()
    posted = _run_pass(mod, [_item(heat=9)], db, ln)
    assert posted == 1 and not ln.submitted
    assert db._execute("SELECT destination FROM posted_articles").fetchone()["destination"] == "channel"


def test_hq_fetch_failure_holds_hot_posts_cold(pipeline):
    mod, db, state = pipeline
    monkeypatch_hq = lambda limit=300, hours=168: None
    mod.fetch_bot_hq_recent_headlines, orig = monkeypatch_hq, mod.fetch_bot_hq_recent_headlines
    try:
        ln = _FakeLN()
        posted = _run_pass(mod, [
            _item(url="https://hot.com/1", heat=9, hint="Aave frozen exploit"),
            _item(url="https://cold.com/2", heat=4, hint="Sol funding round"),
        ], db, ln)
        assert posted == 1                       # cold one only
        assert not ln.submitted
        rows = db._execute("SELECT url, destination FROM posted_articles").fetchall()
        assert [(r["url"], r["destination"]) for r in rows] == [("https://cold.com/2", "channel")]
        # hot story saved NOTHING → retryable later
        assert state["sleeps"] == [2, 4]         # 3 attempts, bounded backoff
    finally:
        mod.fetch_bot_hq_recent_headlines = orig


def test_hot_channel_fails_ln_still_submits(pipeline):
    mod, db, state = pipeline
    state["channel_result"] = (False, None)
    ln = _FakeLN()
    posted = _run_pass(mod, [_item(heat=9)], db, ln)
    assert posted == 1 and ln.submitted
    assert db._execute("SELECT destination FROM posted_articles").fetchone()["destination"] == "ln"


def test_hot_ln_fails_channel_stands(pipeline):
    mod, db, state = pipeline
    ln = _FakeLN(submit_ok=False)
    posted = _run_pass(mod, [_item(heat=9)], db, ln)
    assert posted == 1
    assert db._execute("SELECT destination FROM posted_articles").fetchone()["destination"] == "channel"


def test_cold_channel_fails_saves_nothing(pipeline):
    mod, db, state = pipeline
    state["channel_result"] = (False, None)
    ln = _FakeLN()
    posted = _run_pass(mod, [_item(heat=5)], db, ln)
    assert posted == 0
    assert db._execute("SELECT COUNT(*) c FROM posted_articles").fetchone()["c"] == 0


def test_channel_posts_disabled_cold_story_goes_nowhere(pipeline, monkeypatch):
    mod, db, state = pipeline
    monkeypatch.setattr(mod, "ENABLE_CHANNEL_POSTS", False)
    ln = _FakeLN()
    posted = _run_pass(mod, [_item(heat=5)], db, ln)
    assert posted == 0 and not state["channel_calls"] and not ln.submitted


def test_channel_posts_disabled_hot_story_ln_only(pipeline, monkeypatch):
    mod, db, state = pipeline
    monkeypatch.setattr(mod, "ENABLE_CHANNEL_POSTS", False)
    ln = _FakeLN()
    posted = _run_pass(mod, [_item(heat=9)], db, ln)
    assert posted == 1 and not state["channel_calls"] and ln.submitted
    assert db._execute("SELECT destination FROM posted_articles").fetchone()["destination"] == "ln"


def test_self_dedup_still_blocks_everything(pipeline):
    mod, db, state = pipeline
    db.save_posted(url="https://other.com/same", headline="Aave exploit drains funds fast",
                   story_hint="Aave exploit fund", destination="channel")
    ln = _FakeLN()
    posted = _run_pass(mod, [_item(heat=9, hint="Aave exploit fund")], db, ln)
    assert posted == 0 and not state["channel_calls"] and not ln.submitted


@pytest.mark.parametrize("previous,hint", [
    ("[**$SI** listed on **MEXC** spot](", "[**$MASKSOL** listed on **MEXC** futures]("),
    ("Bitget hack on-chain tracking", "Circle and Tether freeze $318,000 linked to Bitget hack"),
])
@pytest.mark.parametrize("published", [False, True])
def test_jev_publication_uses_event_identity_despite_word_overlap(
        pipeline, monkeypatch, previous, hint, published):
    mod, db, state = pipeline
    monkeypatch.setenv("JEV_TASKS", "news")
    db.save_posted("https://other.com/previous", previous if published else "[self-duplicate]",
                   story_hint=previous, destination="channel" if published else None)
    assert db.was_story_posted(hint)  # The old heuristic wrongly stopped this candidate.
    checked = []
    def semantic_check(self, candidate, earlier):
        checked.append((candidate, earlier))
        return False
    monkeypatch.setattr(mod.jev.NewsEvaluator, "published_duplicate", semantic_check)
    assert _run_pass(mod, [_item(hint=hint)], db, _FakeLN()) == 1
    assert checked == [("Crafted headline about the story.", [previous] if published else [])]
    assert len(state["channel_calls"]) == 1


def test_translated_source_is_checked_again_after_headline_crafting(pipeline, monkeypatch):
    mod, db, state = pipeline
    monkeypatch.setenv("JEV_TASKS", "news")
    monkeypatch.setattr(mod, "ENABLE_WIRE_SUBMISSIONS", True)
    old = "Acurast details ACU token allocations, vesting and governance in Blockworks transparency filing"
    new = "Acurast details ACU token allocations and governance in Blockworks filing, which lists two disclosure gaps"
    db.save_posted("https://acurast.com/ko/filing", old, story_hint="철저한 투명성",
                   destination="both", ln_article_id=11, tg_message_id=1543)
    monkeypatch.setattr(mod, "resolve_craft_headline", lambda u, t: (u, new))
    seen = []
    def duplicate(self, candidate, earlier):
        seen.append((candidate, earlier))
        return old in earlier
    monkeypatch.setattr(mod.jev.NewsEvaluator, "published_duplicate", duplicate, raising=False)
    ln = _FakeLN()
    item = _item("https://acurast.com/fr/filing", heat=2,
                 hint="Transparence radicale : pourquoi Acurast a publié le Token Transparency Filing de l’ACU")
    assert _run_pass(mod, [item], db, ln) == 0
    assert seen == [(new, [old])]
    assert not state["channel_calls"] and not ln.submitted
    assert db.was_url_posted(item["url"])  # terminal duplicate, not a retry loop


def test_final_dedup_failure_holds_without_negative_marker(pipeline, monkeypatch):
    mod, db, state = pipeline
    monkeypatch.setenv("JEV_TASKS", "news")
    def unavailable(*a): raise mod.jev.JevError("HTTP 503")
    monkeypatch.setattr(mod.jev.NewsEvaluator, "published_duplicate", unavailable, raising=False)
    ln = _FakeLN()
    assert _run_pass(mod, [_item()], db, ln) == 0
    assert not state["channel_calls"] and not ln.submitted
    assert not db.was_url_posted("https://src.com/story")


def test_final_dedup_check_and_publication_are_serialized(pipeline, monkeypatch):
    mod, db, state = pipeline
    monkeypatch.setenv("JEV_TASKS", "news")
    barrier = threading.Barrier(2)
    def craft(url, text):
        barrier.wait(timeout=5)
        return url, "Same filing announced in two languages"
    monkeypatch.setattr(mod, "resolve_craft_headline", craft)
    monkeypatch.setattr(mod.jev.NewsEvaluator, "published_duplicate",
                        lambda self, candidate, earlier: candidate in earlier, raising=False)
    ln = _FakeLN()
    items = [_item("https://src.com/fr/a", hint="Texte français distinct"),
             _item("https://src.com/ko/a", hint="한국어 소식")]
    assert _run_pass(mod, items, db, ln) == 1
    assert len(state["channel_calls"]) == 1


def test_published_history_excludes_markers_and_keeps_more_than_25(agent):
    mod, db = agent
    for i in range(30):
        db.save_posted(f"https://src.com/{i}", f"Published news {i}", destination="channel")
    db.save_posted("https://src.com/blocked", "[blocked source]", story_hint="Unpublished story")
    db.save_posted("https://src.com/duplicate", "[self-duplicate]", story_hint="Unpublished claim")
    prior = db.recent_published_headlines()
    assert len(prior) == 30
    assert set(prior) == {f"Published news {i}" for i in range(30)}


def test_stale_article_is_retired_without_repeating_freshness_or_faking_publication(pipeline, monkeypatch):
    mod, db, state = pipeline
    calls = []
    monkeypatch.setattr(mod, "check_article_freshness",
                        lambda u, t: calls.append(u) or False)
    monkeypatch.setattr(mod, "resolve_craft_headline",
                        lambda *a: pytest.fail("stale articles need no headline"))
    item = _item()
    assert _run_pass(mod, [item], db, _FakeLN()) == 0
    assert _run_pass(mod, [item], db, _FakeLN()) == 0
    assert calls == [item["url"]]
    assert db.was_url_posted(item["url"])
    assert db.recent_published_headlines() == []
    assert not db.was_story_posted(item["headline_hint"])
    assert state["channel_calls"] == []


@pytest.mark.parametrize("resolved_state", ["published", "rejected", "blocked"])
def test_resolved_terminal_url_retires_original_alias(pipeline, monkeypatch, resolved_state):
    mod, db, state = pipeline
    monkeypatch.setenv("JEV_TASKS", "news")
    canonical = "https://canonical.example/story"
    if resolved_state != "blocked":
        db.save_posted(canonical, "Existing article" if resolved_state == "published" else "[self-duplicate]",
                       destination="channel" if resolved_state == "published" else None)
    else:
        monkeypatch.setattr(mod, "is_blocked_source", lambda url: url == canonical)
    calls = []
    monkeypatch.setattr(mod, "resolve_craft_headline",
                        lambda url, text: calls.append(url) or (canonical, "Crafted headline"))
    item = _item()
    assert _run_pass(mod, [dict(item)], db, _FakeLN()) == 0
    assert db.was_url_posted(item["url"])
    assert _run_pass(mod, [dict(item)], db, _FakeLN()) == 0
    assert calls == [item["url"]]
    assert db.recent_published_headlines() == (["Existing article"] if resolved_state == "published" else [])
    alias = db._execute("SELECT * FROM posted_articles WHERE url=?", (item["url"],)).fetchone()
    assert not alias["destination"] and not alias["ln_article_id"] and not alias["tg_message_id"]
    assert not alias["story_hint"]
    assert not state["channel_calls"]


def test_no_eligible_destination_skips_freshness_and_craft(pipeline, monkeypatch):
    """Rollback config (ENABLE_CHANNEL_POSTS=0) with the default heat bar leaves
    cold stories with nowhere to land — the pass must not spend the freshness
    and creative craft calls on a headline nothing can publish."""
    mod, db, state = pipeline
    monkeypatch.setattr(mod, "ENABLE_CHANNEL_POSTS", False)
    crafts, freshness = [], []
    monkeypatch.setattr(mod, "resolve_craft_headline",
                        lambda u, t: crafts.append(u) or (u, "Crafted headline about it."))
    monkeypatch.setattr(mod, "check_article_freshness",
                        lambda u, t: freshness.append(u) or True)
    ln = _FakeLN()
    assert _run_pass(mod, [_item(heat=5)], db, ln) == 0
    assert not crafts and not freshness


def test_unknown_heat_stores_null_not_zero(pipeline):
    """heat=0 means 'the evaluator gave no score' — storing a literal 0 would
    make an unscored story indistinguishable from a genuinely ice-cold one."""
    mod, db, state = pipeline
    ln = _FakeLN()
    assert _run_pass(mod, [_item(heat=0)], db, ln) == 1
    assert db._execute("SELECT heat FROM posted_articles").fetchone()["heat"] is None
    assert not ln.submitted


def test_ln_submit_exception_does_not_orphan_the_channel_post(pipeline):
    """`LNClient.submit_article` is the one sibling with no internal try/except:
    transport errors, a 200 with a non-JSON body, and a re-auth failure all
    propagate. The submit runs FIRST now (its response carries the channel
    link), so the scoped try/except is what keeps an escaping exception from
    taking the channel leg down with it — the story must still ship linkless
    and land in posted_articles so both universal-gate dedup checks cover it."""
    mod, db, state = pipeline

    class _RaisingLN(_FakeLN):
        def submit_article(self, url, headline, market_id=None, wire=False):
            raise ConnectionError("read timed out to LN API")

    ln = _RaisingLN()
    posted = _run_pass(mod, [_item(heat=9)], db, ln)
    assert posted == 1                 # the channel leg landed and was counted
    assert state["channel_calls"]
    row = db._execute("SELECT destination, tg_message_id, ln_article_id "
                      "FROM posted_articles").fetchone()
    assert (row["destination"], row["tg_message_id"], row["ln_article_id"]) == \
        ("channel", 42, None)
    assert ln.votes == []              # nothing to upvote — no article id
    assert db.was_url_posted("https://src.com/story")     # dedup now covers it
    assert db.was_story_posted("Aave exploit fund")


# ── LN-first ordering: the channel message carries the article link ──────

def test_hot_story_channel_message_carries_canonical_link(pipeline):
    mod, db, state = pipeline
    ln = _FakeLN(canonical="https://leviathannews.xyz/555/some-slug")
    posted = _run_pass(mod, [_item(heat=9)], db, ln)
    assert posted == 1 and ln.submitted and state["channel_calls"]
    assert state["ln_links"] == ["https://leviathannews.xyz/555/some-slug"]
    assert db._execute(
        "SELECT destination FROM posted_articles").fetchone()["destination"] == "both"


def test_submit_runs_before_channel_send(pipeline):
    mod, db, state = pipeline
    order = []
    ln = _FakeLN(canonical="https://leviathannews.xyz/555/s")
    real_submit = ln.submit_article
    ln.submit_article = lambda *a, **k: (order.append("ln"), real_submit(*a, **k))[1]
    fixture_channel = mod.post_to_channel  # the fixture fake

    def tracking_channel(headline, url, ln_url=None, ln_story_url=None):
        order.append("channel")
        return fixture_channel(headline, url, ln_url=ln_url,
                               ln_story_url=ln_story_url)
    mod.post_to_channel = tracking_channel
    try:
        _run_pass(mod, [_item(heat=9)], db, ln)
    finally:
        mod.post_to_channel = fixture_channel
    assert order == ["ln", "channel"]


def test_submit_failure_still_posts_channel_without_link(pipeline):
    mod, db, state = pipeline
    ln = _FakeLN(submit_ok=False, canonical="https://leviathannews.xyz/555/s")
    posted = _run_pass(mod, [_item(heat=9)], db, ln)
    assert posted == 1 and state["channel_calls"]
    assert state["ln_links"] == [None]
    assert db._execute(
        "SELECT destination FROM posted_articles").fetchone()["destination"] == "channel"


def test_missing_canonical_posts_linkless_but_both(pipeline):
    mod, db, state = pipeline
    ln = _FakeLN(canonical=None)  # real API always nests news{}, may lack slug
    posted = _run_pass(mod, [_item(heat=9)], db, ln)
    assert posted == 1 and state["ln_links"] == [None]
    assert db._execute(
        "SELECT destination FROM posted_articles").fetchone()["destination"] == "both"


def test_cold_story_channel_message_has_no_link(pipeline):
    mod, db, state = pipeline
    ln = _FakeLN(canonical="https://leviathannews.xyz/555/s")
    posted = _run_pass(mod, [_item(heat=2)], db, ln)
    assert posted == 1 and not ln.submitted
    assert state["ln_links"] == [None]


def test_garbage_canonical_url_is_rejected_not_sent(pipeline):
    mod, db, state = pipeline
    ln = _FakeLN(canonical="https://leviathannews.xyz/x\ny-injected")
    posted = _run_pass(mod, [_item(heat=9)], db, ln)
    assert posted == 1 and state["ln_links"] == [None]  # validate_url rejects


# ── Wire tier (squid-bot PR #882, agent side) ────────────────────────────
# Dual path (operator decision 2026-08-08): hot stories keep the editorial
# submission; cold + LN-gate-demoted stories become instant wire rows. Both
# legs return format_news_data, whose "url" is the skip-landing tracked
# redirect — channel posts link Leviathan, never the raw source, whenever an
# LN row exists.

_REDIRECT = "https://leviathannews.xyz/redirect/555?utm_medium=api&utm_source=leviathan_api&utm_campaign=news&skip_landing=true"
_CANONICAL = "https://leviathannews.xyz/555/story-slug"


def _fresh_routing_defaults(**env_overrides):
    """Import ln-agent.py in a fresh interpreter (the knobs parse at import)."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("BENTHIC_NEWS_CHANNEL", "ENABLE_CHANNEL_POSTS",
                        "ENABLE_WIRE_SUBMISSIONS")}
    env.update(env_overrides)
    out = subprocess.run(
        [sys.executable, "-c",
         "import importlib.util, json, sys\n"
         "spec = importlib.util.spec_from_file_location('m', 'ln-agent.py')\n"
         "m = importlib.util.module_from_spec(spec)\n"
         "sys.modules['m'] = m\n"
         "spec.loader.exec_module(m)\n"
         "print('ROUTE=' + json.dumps([m.BENTHIC_NEWS_CHANNEL, m.ENABLE_CHANNEL_POSTS,"
         " m.ENABLE_WIRE_SUBMISSIONS]))\n"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    line = next(l for l in out.stdout.splitlines() if l.startswith("ROUTE="))
    return json.loads(line.removeprefix("ROUTE="))


def test_routing_defaults_no_channel_and_wire_off():
    """White-label: no channel ships baked in, so channel posting is off until
    BENTHIC_NEWS_CHANNEL is set; wire rows are staff-gated server-side, so the
    tier is opt-in. A fork posts only the editorial (heat-gated) LN leg."""
    assert _fresh_routing_defaults() == ["", False, False]


def test_routing_defaults_channel_configured_enables_posts():
    assert _fresh_routing_defaults(BENTHIC_NEWS_CHANNEL=" @example_news ") == [
        "@example_news", True, False]
    assert _fresh_routing_defaults(BENTHIC_NEWS_CHANNEL="@example_news",
                                   ENABLE_CHANNEL_POSTS="0",
                                   ENABLE_WIRE_SUBMISSIONS="1") == [
        "@example_news", False, True]


def test_no_channel_hot_story_still_reaches_ln_and_cold_skips_craft(pipeline, monkeypatch):
    """The fork default (no channel, wire off): hot stories take the editorial
    LN leg; cold stories bail before any freshness/craft spend and stay
    unrecorded (a later configuration change can still pick them up)."""
    mod, db, state = pipeline
    monkeypatch.setattr(mod, "ENABLE_CHANNEL_POSTS", False)
    crafts = []
    monkeypatch.setattr(mod, "resolve_craft_headline",
                        lambda u, t: crafts.append(u) or (u, "Crafted headline about it."))
    ln = _FakeLN()
    hot = _item(heat=9)
    cold = _item(url="https://example.com/cold-story", heat=5,
                 hint="Bridge protocol adds a new route")
    assert _run_pass(mod, [hot], db, ln) == 1
    assert ln.submitted and ln.wire_flags == [False]
    assert not state["channel_calls"]
    crafts.clear()
    assert _run_pass(mod, [cold], db, ln) == 0
    assert not crafts and not db.was_url_posted(cold["url"])


def test_submit_article_sends_wire_flag_in_body():
    from unittest.mock import MagicMock
    mod = _load_agent_module()
    c = mod.LNClient.__new__(mod.LNClient)
    c.session = MagicMock()
    c._lock = __import__("threading").RLock()
    c._refresh_if_stale = lambda: None
    resp = MagicMock()
    resp.ok = True
    resp.json.return_value = {"success": True, "news": {"id": 7}}
    c.session.post.return_value = resp
    c.submit_article("https://a.com/x", "Headline", wire=True)
    body = c.session.post.call_args.kwargs["json"]
    assert body.get("wire") is True
    c.submit_article("https://a.com/y", "Headline two")
    body2 = c.session.post.call_args.kwargs["json"]
    assert "wire" not in body2  # editorial submissions unchanged


def test_channel_payloads_story_link_is_ln_redirect():
    """With a wire/editorial LN row, the story link swaps to the skip-landing
    redirect — the raw source URL appears NOWHERE in the message."""
    mod = _load_agent_module()
    rich, plain = mod._channel_payloads("Headline.", "https://src.com/x",
                                        _CANONICAL, ln_story_url=_REDIRECT)
    assert "src.com" not in rich["text"] and "src.com" not in plain["text"]
    assert plain["text"] == f"Headline.\n\n{_REDIRECT}"
    assert rich["text"].startswith(
        f'Headline.\n\n<a href="{html.escape(_REDIRECT, quote=True)}">Source</a> ⚓ ')
    assert 'href="' + html.escape(f"{_CANONICAL}?{_UTM}", quote=True) + '"' in rich["text"]


def test_channel_payloads_story_link_without_canonical_is_plain_only():
    mod = _load_agent_module()
    payloads = mod._channel_payloads("Headline.", "https://src.com/x",
                                     None, ln_story_url=_REDIRECT)
    assert payloads == [{"chat_id": mod.BENTHIC_NEWS_CHANNEL,
                         "text": f"Headline.\n\n{_REDIRECT}",
                         "link_preview_options": {"is_disabled": True}}]


def test_cold_story_goes_wire(pipeline, monkeypatch):
    mod, db, state = pipeline
    monkeypatch.setattr(mod, "ENABLE_WIRE_SUBMISSIONS", True)
    ln = _FakeLN(canonical=_CANONICAL, redirect=_REDIRECT)
    posted = _run_pass(mod, [_item(heat=5)], db, ln)
    assert posted == 1
    assert ln.wire_flags == [True]
    # Cold stories still skip the LN editorial gate entirely — wire needs no
    # HQ dedup (instant row, not a main-feed submission).
    assert state["prov_calls"] == 0 and state["hq_llm_calls"] == 0
    assert state["hq_fetch_calls"] == 0
    assert state["story_links"] == [_REDIRECT]
    assert state["ln_links"] == [_CANONICAL]
    assert ln.votes == []  # wire rows never rank the main feed — no self-vote
    row = db._execute("SELECT destination, ln_article_id FROM posted_articles").fetchone()
    assert (row["destination"], row["ln_article_id"]) == ("both", 555)


def test_demoted_story_goes_wire(pipeline, monkeypatch):
    mod, db, state = pipeline
    monkeypatch.setattr(mod, "ENABLE_WIRE_SUBMISSIONS", True)
    state["prov"] = "reject"
    ln = _FakeLN(canonical=_CANONICAL, redirect=_REDIRECT)
    posted = _run_pass(mod, [_item(heat=9)], db, ln)
    assert posted == 1
    assert ln.wire_flags == [True]  # demote → wire, not editorial
    assert state["story_links"] == [_REDIRECT]
    row = db._execute("SELECT destination FROM posted_articles").fetchone()
    assert row["destination"] == "both"


def test_hot_story_editorial_channel_links_redirect(pipeline, monkeypatch):
    mod, db, state = pipeline
    monkeypatch.setattr(mod, "ENABLE_WIRE_SUBMISSIONS", True)
    ln = _FakeLN(canonical=_CANONICAL, redirect=_REDIRECT)
    posted = _run_pass(mod, [_item(heat=9)], db, ln)
    assert posted == 1
    assert ln.wire_flags == [False]  # editorial submission, not wire
    assert ln.votes == [(555, 1)]    # editorial self-upvote preserved
    assert state["story_links"] == [_REDIRECT]


def test_wire_failure_falls_back_to_channel_with_source_link(pipeline, monkeypatch):
    mod, db, state = pipeline
    monkeypatch.setattr(mod, "ENABLE_WIRE_SUBMISSIONS", True)
    ln = _FakeLN(submit_ok=False)
    posted = _run_pass(mod, [_item(heat=5)], db, ln)
    assert posted == 1
    assert ln.wire_flags == [True]
    assert state["story_links"] == [None]  # no LN row → source link fallback
    row = db._execute("SELECT destination FROM posted_articles").fetchone()
    assert row["destination"] == "channel"


def test_wire_exception_falls_back_to_channel(pipeline, monkeypatch):
    mod, db, state = pipeline
    monkeypatch.setattr(mod, "ENABLE_WIRE_SUBMISSIONS", True)

    class _RaisingWire(_FakeLN):
        def submit_article(self, url, headline, market_id=None, wire=False):
            raise ConnectionError("read timed out to LN API")

    posted = _run_pass(mod, [_item(heat=5)], db, _RaisingWire())
    assert posted == 1
    row = db._execute("SELECT destination FROM posted_articles").fetchone()
    assert row["destination"] == "channel"


def test_wire_off_is_exact_legacy(pipeline):
    """Kill-switch contract: ENABLE_WIRE_SUBMISSIONS=0 restores channel-only
    cold stories with the raw source link."""
    mod, db, state = pipeline
    ln = _FakeLN(canonical=_CANONICAL, redirect=_REDIRECT)
    posted = _run_pass(mod, [_item(heat=5)], db, ln)
    assert posted == 1 and not ln.submitted
    assert state["story_links"] == [None]
    row = db._execute("SELECT destination FROM posted_articles").fetchone()
    assert row["destination"] == "channel"


def test_wire_channel_off_still_posts_wire(pipeline, monkeypatch):
    """Channel kill-switch off + wire on: the wire leg alone must still carry
    the story (the old early-bail assumed LN-ineligible == nothing to do)."""
    mod, db, state = pipeline
    monkeypatch.setattr(mod, "ENABLE_WIRE_SUBMISSIONS", True)
    monkeypatch.setattr(mod, "ENABLE_CHANNEL_POSTS", False)
    ln = _FakeLN(canonical=_CANONICAL, redirect=_REDIRECT)
    posted = _run_pass(mod, [_item(heat=5)], db, ln)
    assert posted == 1
    assert ln.wire_flags == [True]
    assert state["channel_calls"] == []
    row = db._execute("SELECT destination FROM posted_articles").fetchone()
    assert row["destination"] == "ln"
