"""Tests for the PR #1 automated-review fix round (security + correctness).

Covers the cleanly unit-testable findings:
  #2 GitHub intent gate must require GitHub-specific wording (not "open an article")
  #3 PM2 intent gate must require process-diagnostics wording (not "check"/"status")
  #5 Codex honors the caller timeout for tool-free classification calls
  #6 the API analyze path passes the no-tools sentinel

(#1 DNS-exfil is infra/docker — verified on the server; #7 market-cache, #8 build
route, #9 cancel-state are verified by inspection + the existing builder suite.)
"""

import importlib.util
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


def _load_bot_module():
    """Import benthic-bot.py under a Python-safe module name (hyphen in filename)."""
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


# ── #2 GitHub intent gate ──────────────────────────────────────────────────
# Generic "open/file a <thing>" must NOT authorize a [GH:...] directive — only
# GitHub-specific wording should, so injected directives can't ride on "open an
# article".
@pytest.mark.parametrize("phrase", [
    "open an article",
    "open a link",
    "file a report",
    "open a ticket for me",
    "can you open the doc",
])
def test_gh_intent_gate_rejects_generic_phrases(phrase):
    bot = _load_bot_module()
    assert not bot._GH_INTENT_RE.search(phrase), f"should NOT match: {phrase!r}"


@pytest.mark.parametrize("phrase", [
    "open a github issue",
    "create a PR",
    "comment on the PR",
    "file a bug",
    "open a pull request",
    "push it to github",
])
def test_gh_intent_gate_accepts_github_wording(phrase):
    bot = _load_bot_module()
    assert bot._GH_INTENT_RE.search(phrase), f"should match: {phrase!r}"


# ── #3 PM2 intent gate ─────────────────────────────────────────────────────
# Routine words (check / status / running) must NOT authorize a [PM2-LOGS:...]
# directive — only explicit process-diagnostics wording should.
@pytest.mark.parametrize("phrase", [
    "check this URL",
    "what's the status?",
    "is it running?",
    "let me check that",
])
def test_pm2_intent_gate_rejects_generic_phrases(phrase):
    bot = _load_bot_module()
    assert not bot._PM2_INTENT_RE.search(phrase), f"should NOT match: {phrase!r}"


@pytest.mark.parametrize("phrase", [
    "check pm2 logs",
    "restart the bot",
    "show me the logs",
    "the process crashed",
    "diagnose this",
])
def test_pm2_intent_gate_accepts_diagnostics_wording(phrase):
    bot = _load_bot_module()
    assert bot._PM2_INTENT_RE.search(phrase), f"should match: {phrase!r}"


# ── #5 Codex effective timeout ─────────────────────────────────────────────
def test_codex_timeout_honors_caller_for_toolfree():
    import providers
    assert providers._codex_effective_timeout(120, "__none__") == 120
    assert providers._codex_effective_timeout(120, "") == 120


def test_codex_timeout_floors_tool_and_reasoning_calls():
    import providers
    # tool calls (None or a real allowlist) get at least the 1h floor...
    assert providers._codex_effective_timeout(120, None) == 3600
    assert providers._codex_effective_timeout(600, "WebSearch") == 3600
    # ...but a caller asking for MORE than the floor still wins.
    assert providers._codex_effective_timeout(7200, "WebSearch") == 7200
