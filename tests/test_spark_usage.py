"""Tests for spark-usage reductions: bot notification gate + agent provenance dedup.

Context (2026-07-02): the weekly gpt-5.3-codex-spark budget was burning at ~130%
of sustainable pace. Measured drivers: ~2k bot pre-screens/day (27% of them
mechanical lnn_headline_bot notifications) and ~56 HQ dup-checks/day at ~8.2k
input tokens each. The notification gate answers the first without an LLM; the
provenance API answers the second without an LLM.
"""

import importlib.util
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


# ─── module loaders (cached, shared shape with the other test files) ─────────

def _load_bot_module():
    os.environ.setdefault("BENTHIC_BOT_TOKEN", "test:stub-token-do-not-use")
    os.environ.setdefault("WALLET_PRIVATE_KEY", "")
    if "benthic_bot_spark_test" in sys.modules:
        return sys.modules["benthic_bot_spark_test"]
    spec = importlib.util.spec_from_file_location(
        "benthic_bot_spark_test", ROOT / "benthic-bot.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["benthic_bot_spark_test"] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_agent_module():
    if "ln_agent_spark_test" in sys.modules:
        return sys.modules["ln_agent_spark_test"]
    spec = importlib.util.spec_from_file_location(
        "ln_agent_spark_test", ROOT / "ln-agent.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ln_agent_spark_test"] = mod
    spec.loader.exec_module(mod)
    return mod


# ─── Fix A: deterministic notification gate (benthic-bot) ────────────────────

@pytest.mark.parametrize("text", [
    "🚀 Production Deploy\nf1b29889 on main\nfeat(linkedin): Phase B",
    "Push to leviathan-news/auction-ui/main by zcor",
    "📢 PR #555 opened\n[Packet B][opus] T1: Atlas view beacon dedu",
    "✅ PR #511 merged\nfeat(linkedin): Phase B posting spine + nat",
    "「 ✦ ADMIN PANEL __(Updated 12:34 AM PT)__ ✦ 」\n📝 Submitted",
    "Open Prediction Markets\n#1 Will X happen",
])
def test_notification_gate_matches_routine_lnn_shapes(text):
    bot = _load_bot_module()
    assert bot._is_routine_notification("lnn_headline_bot", text) is True


@pytest.mark.parametrize("username,text", [
    # Real questions/statements from lnn must still reach the pre-screen.
    ("lnn_headline_bot", "what do you think about the new market?"),
    ("lnn_headline_bot", "Benthic your article got approved"),
    # Same shapes from OTHER senders are not gated (lnn-only scope).
    ("zcor", "🚀 Production Deploy"),
    ("shark_bot", "Push to leviathan-news/auction-ui/main by zcor"),
    ("", "📢 PR #12 opened"),
    # PR-shaped text mid-message (not a notification prefix) is not gated.
    ("lnn_headline_bot", "did you see 📢 PR #555 opened by zcor?"),
])
def test_notification_gate_leaves_everything_else_alone(username, text):
    bot = _load_bot_module()
    assert bot._is_routine_notification(username, text) is False


# ─── Fix B: provenance-first dup check (ln-agent) ────────────────────────────

class _Resp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body or {}

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


@pytest.fixture
def agent(monkeypatch):
    mod = _load_agent_module()
    monkeypatch.setattr(mod, "ENABLE_PROVENANCE_DEDUP", True)
    return mod


def _iso(hours_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


def test_provenance_duplicate_rejects(agent, monkeypatch):
    monkeypatch.setattr(agent.requests, "post",
                        lambda *a, **k: _Resp(body={"verdict": "duplicate", "match_count": 3}))
    assert agent._provenance_dedup_check("https://x.com/a", "hint") == "reject"


def test_provenance_new_and_stale_fall_back_to_hq_check(agent, monkeypatch):
    """'new'/'stale' are NOT trusted: live validation (2026-07-02) showed the
    provenance index returns 'new' for our own approved articles (exact URL and
    exact headline of #267269, approved 10h earlier — 0 matches; /search also
    blind to it). A false 'new' means posting a duplicate, so absence-of-match
    must still run the spark HQ check. Only positive matches are trusted."""
    for verdict in ("new", "stale"):
        monkeypatch.setattr(agent.requests, "post",
                            lambda *a, verdict=verdict, **k: _Resp(body={"verdict": verdict}))
        assert agent._provenance_dedup_check("https://x.com/a", "hint") == "fallback"


def test_provenance_known_recent_rejects_old_proceeds(agent, monkeypatch):
    monkeypatch.setattr(agent.requests, "post",
                        lambda *a, **k: _Resp(body={"verdict": "known", "latest_seen": _iso(2)}))
    assert agent._provenance_dedup_check("https://x.com/a", "hint", recent_hours=168) == "reject"

    monkeypatch.setattr(agent.requests, "post",
                        lambda *a, **k: _Resp(body={"verdict": "known", "latest_seen": _iso(400)}))
    assert agent._provenance_dedup_check("https://x.com/a", "hint", recent_hours=168) == "proceed"


def test_provenance_known_unparsable_date_rejects(agent, monkeypatch):
    monkeypatch.setattr(agent.requests, "post",
                        lambda *a, **k: _Resp(body={"verdict": "known", "latest_seen": "not-a-date"}))
    assert agent._provenance_dedup_check("https://x.com/a", "hint") == "reject"


@pytest.mark.parametrize("resp", [
    _Resp(status=500),
    _Resp(status=429),
    _Resp(body={"verdict": "weird-new-verdict"}),
    _Resp(body=ValueError("not json")),
])
def test_provenance_inconclusive_falls_back(agent, monkeypatch, resp):
    monkeypatch.setattr(agent.requests, "post", lambda *a, **k: resp)
    assert agent._provenance_dedup_check("https://x.com/a", "hint") == "fallback"


def test_provenance_network_error_falls_back(agent, monkeypatch):
    def boom(*a, **k):
        raise agent.requests.exceptions.ConnectionError("down")
    monkeypatch.setattr(agent.requests, "post", boom)
    assert agent._provenance_dedup_check("https://x.com/a", "hint") == "fallback"


def test_provenance_disabled_or_empty_falls_back(agent, monkeypatch):
    monkeypatch.setattr(agent, "ENABLE_PROVENANCE_DEDUP", False)
    assert agent._provenance_dedup_check("https://x.com/a", "hint") == "fallback"
    monkeypatch.setattr(agent, "ENABLE_PROVENANCE_DEDUP", True)
    assert agent._provenance_dedup_check("", "") == "fallback"


def test_provenance_sends_url_and_truncated_text(agent, monkeypatch):
    seen = {}

    def capture(url, json=None, timeout=None, **k):
        seen["url"] = url
        seen["payload"] = json
        return _Resp(body={"verdict": "new"})

    monkeypatch.setattr(agent.requests, "post", capture)
    agent._provenance_dedup_check("https://x.com/a", "h" * 500)
    assert seen["url"] == agent.PROVENANCE_CHECK_URL
    assert seen["payload"]["url"] == "https://x.com/a"
    assert len(seen["payload"]["text"]) == 300


def test_identity_uses_runtime_mediated_sandbox_directive():
    from prompt_loader import load_prompt
    identity = load_prompt("bot/identity", agent_name="Benthic",
                           bot_username="benthic_bot", AGENT_DIR="/opt/agent")
    assert "[SANDBOX]" in identity
    assert "[/SANDBOX]" in identity
    assert "Trusted bot Python" in identity
    assert "run-sandbox.sh to execute" not in identity
    assert "DIRECT shell command" not in identity


def test_codex_wrapper_denies_direct_docker_and_emits_chat_directive():
    from prompt_loader import load_prompt
    wrapper = load_prompt("bot/codex_wrapper", agent_name="Benthic",
                          AGENT_DIR="/opt/agent", prompt="x")
    assert "intentionally unavailable" in wrapper
    assert "[SANDBOX]" in wrapper
    assert "Trusted bot Python" in wrapper
    assert "sandbox/run-sandbox.sh '" not in wrapper
    assert "strict JSON" in wrapper
    assert "already supplies an UNTRUSTED SANDBOX RESULT" in wrapper
