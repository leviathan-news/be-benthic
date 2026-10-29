"""Quota-burn controls on the news evaluation pipeline (2026-08-08).

Root cause: evaluate_and_deduplicate ran through the legacy claude_ask wrapper
— creative default (sol/xhigh + soul + web-search researcher prompt) — while
the push mini-pass multiplied its call count ~10x. One day of that (Aug 3)
exhausted the entire weekly GPT-5.6 pool. These tests pin the cost controls:

1. Evaluation runs at classification tier, soul-free, hard text-only.
2. Operator-listed firehose sources never wake the news mini-pass (hourly only).
3. Mini-pass throttle defaults: fewer, fatter batches.
4. The eval prompt no longer instructs tool use (research lives in
   resolve_craft_headline, which keeps its WebFetch/WebSearch workflow).
"""

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


def _load_agent_module(name="ln_agent_push_test"):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, ROOT / "ln-agent.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def agent(tmp_path):
    mod = _load_agent_module()
    db = mod.AgentDB(db_path=tmp_path / "test.db")
    yield mod, db
    db.close()


def _msg(mid=1, channel="@src", text="Breaking: exploit confirmed https://a.com/story"):
    return {"channel": channel, "id": mid, "text": text,
            "date": "2026-08-08T10:00:00+00:00", "is_group": False}


# ── 1. Evaluation tier ───────────────────────────────────────────────────

def test_news_agent_codex_models(agent):
    mod, _db = agent
    creative = mod._codex_provider.resolved_call()
    classification = mod._codex_provider.resolved_call(tier="classification")
    assert (creative.model, creative.effort) == ("gpt-6-sol", "xhigh")
    assert (classification.model, classification.effort) == ("gpt-6-luna", "medium")


def test_eval_uses_classification_tier_no_soul_no_tools(agent, monkeypatch):
    mod, db = agent
    calls = []

    def fake_llm_ask(prompt, timeout=3600, **kwargs):
        calls.append(kwargs)
        return json.dumps([{"msg_id": 1, "channel": "@src",
                            "url": "https://a.com/story",
                            "headline_hint": "exploit", "heat": 6,
                            "reason": "confirmed exploit"}])

    monkeypatch.setattr(mod, "llm_ask", fake_llm_ask)
    results = mod.evaluate_and_deduplicate([_msg()], db)
    assert len(results) == 1 and results[0]["heat"] == 6
    assert len(calls) == 1
    assert calls[0].get("tier") == "classification"
    assert calls[0].get("skip_soul") is True
    assert calls[0].get("tools") == "__none__"


def test_eval_retry_uses_classification_tier(agent, monkeypatch):
    mod, db = agent
    calls = []
    responses = iter([
        "I think the newsworthy item is the exploit {not json",
        json.dumps([{"msg_id": 1, "channel": "@src",
                     "url": "https://a.com/story",
                     "headline_hint": "exploit", "heat": 5, "reason": "r"}]),
    ])

    def fake_llm_ask(prompt, timeout=3600, **kwargs):
        calls.append(kwargs)
        return next(responses)

    monkeypatch.setattr(mod, "llm_ask", fake_llm_ask)
    results = mod.evaluate_and_deduplicate([_msg()], db)
    assert len(results) == 1
    assert len(calls) == 2
    for kw in calls:
        assert kw.get("tier") == "classification"
        assert kw.get("skip_soul") is True
        assert kw.get("tools") == "__none__"


# ── 2. Push exclusion for firehose sources ───────────────────────────────

def test_register_source_handlers_skips_push_excluded(agent, monkeypatch):
    mod, db = agent
    db.save_channel_id("@one", 1000000111, "One", "channel")
    db.save_channel_id("@noisy", 1000000222, "Noisy", "channel")
    monkeypatch.setattr(mod, "CHANNELS", ["@one", "@noisy"])
    monkeypatch.setattr(mod, "ID_SOURCES", {-1002000000333: "Noisy Feed",
                                            -1002000000444: "Real Feed"})
    monkeypatch.setattr(mod, "PUSH_EXCLUDED_SOURCES",
                        {"@noisy", "Noisy Feed"})

    async def fake_resolve(client, channel, _db):
        return _db.get_channel_id(channel)
    monkeypatch.setattr(mod, "resolve_channel", fake_resolve)

    registered = {}

    class FakeClient:
        def add_event_handler(self, cb, event_filter):
            registered["filter"] = event_filter

        async def get_entity(self, src_id):
            return SimpleNamespace(megagroup=False)

    n = asyncio.run(mod.register_source_handlers(FakeClient(), db))
    assert n == 2  # @one + Real Feed only

    from telethon import utils
    from telethon.tl import types
    marked_one = utils.get_peer_id(types.PeerChannel(1000000111))
    assert sorted(registered["filter"].chats) == sorted(
        [marked_one, -1002000000444])


@pytest.mark.parametrize("raw,expected", [
    (None, set()),
    ("@noisy, Noisy Feed ,", {"@noisy", "Noisy Feed"}),
    ("", set()),
])
def test_push_exclusions_default_empty_and_env_override(raw, expected):
    """White-label: no curated firehose list ships. PUSH_EXCLUDED_SOURCES is
    parsed at import, so each case runs in a fresh interpreter; an env value
    replaces the (empty) default entirely."""
    import os
    import subprocess
    env = {k: v for k, v in os.environ.items() if k != "PUSH_EXCLUDED_SOURCES"}
    if raw is not None:
        env["PUSH_EXCLUDED_SOURCES"] = raw
    out = subprocess.run(
        [sys.executable, "-c",
         "import importlib.util, json, sys\n"
         "spec = importlib.util.spec_from_file_location('m', 'ln-agent.py')\n"
         "m = importlib.util.module_from_spec(spec)\n"
         "sys.modules['m'] = m\n"
         "spec.loader.exec_module(m)\n"
         "print('EXCL=' + json.dumps(sorted(m.PUSH_EXCLUDED_SOURCES)))\n"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    line = next(l for l in out.stdout.splitlines() if l.startswith("EXCL="))
    assert set(json.loads(line.removeprefix("EXCL="))) == expected


# ── 3. Mini-pass throttle defaults ───────────────────────────────────────

def test_news_pass_throttle_defaults():
    mod = _load_agent_module()
    assert mod.NEWS_PASS_MIN_INTERVAL == 900
    assert mod.NEWS_PASS_MAX_MESSAGES == 25


def test_news_pass_max_messages_clamped_to_positive(monkeypatch):
    """A negative cap would reach SQLite as LIMIT -1 == unlimited — the
    exact opposite of a cap (2026-08-02 review follow-up #6)."""
    monkeypatch.setenv("NEWS_PASS_MAX_MESSAGES", "-3")
    mod = _load_agent_module("ln_agent_clamp_test")
    try:
        assert mod.NEWS_PASS_MAX_MESSAGES == 1
    finally:
        sys.modules.pop("ln_agent_clamp_test", None)


# ── 4. Eval prompt is text-only judgment ─────────────────────────────────

def test_eval_prompts_do_not_instruct_tool_use():
    for name in ("evaluate_and_deduplicate", "evaluate_and_deduplicate_retry"):
        text = (ROOT / "prompts" / "agent" / f"{name}.md").read_text()
        for marker in ("WebSearch", "WebFetch", "USE THEM", "twitter-explorer"):
            assert marker not in text, f"{name}.md still instructs tool use: {marker}"
        # The URL contract without tools: echo a URL from the message text.
        assert "message text" in text.lower()
