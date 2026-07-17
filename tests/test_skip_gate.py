"""Regression tests for Benthic chat control-token and identity-leak gates."""

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


@pytest.mark.parametrize("response", [
    "SKIP",
    " skip ",
    "**SKIP**",
    "`SKIP`",
    "SKIP.",
    "PASS",
    "pass",
    "SKIP\n\n(routine PR notification, nothing to add)",
])
def test_control_token_only_accepts_standalone_skip_or_pass(response):
    bot = _load_bot_module()

    assert bot._is_control_token_only(response)


@pytest.mark.parametrize("response", [
    "I'll pass on that one",
    "Skip the hype — the real number is 1.2M",
    "Let's move on to the next market",
    (
        "This is benthic-bot's group-reply decision prompt for an incoming message "
        "— but I'm the interactive Claude Code session in /path/to/agent-dir, "
        "not the live bot process."
    ),
])
def test_control_token_only_rejects_real_replies_and_leaked_essays(response):
    bot = _load_bot_module()

    assert not bot._is_control_token_only(response)


@pytest.mark.parametrize("response", [
    "Affirmative SKIP",                              # the named [37] affirmation wrapper
    "SKIP — routine notification, nothing to add",   # token + same-line reason
    "Sure, SKIP",
    "Yes — PASS",
])
def test_control_token_only_catches_affirmation_wrapped_token(response):
    """[37] 'Affirmative SKIP' class: a SHORT response wrapping a standalone
    UPPERCASE control token in affirmation/explanation must still suppress."""
    bot = _load_bot_module()

    assert bot._is_control_token_only(response)


@pytest.mark.parametrize("response", [
    "pass on that one",                              # lowercase word = legit decline
    "Skip the hype, the real number is 1.2M",        # lowercase 'Skip', not the token
    # Long, legitimate explanation that names the token (e.g. operator diagnostics):
    ("When the model has nothing to add it should emit the control token SKIP and "
     "the gate then suppresses it, which is how the pre-screen avoids posting noise "
     "to the group on every routine notification."),
])
def test_control_token_only_preserves_lowercase_and_long_explanations(response):
    bot = _load_bot_module()

    assert not bot._is_control_token_only(response)


def test_identity_leak_detects_live_prompt_leak_snippet():
    bot = _load_bot_module()
    leaked = (
        "This is benthic-bot's group-reply decision prompt for an incoming message "
        "— but I'm the interactive Claude Code session in /path/to/agent-dir, "
        "not the live bot process."
    )

    assert bot.check_identity_leak(leaked)


def test_identity_leak_allows_normal_grounded_reply():
    bot = _load_bot_module()

    assert not bot.check_identity_leak(
        "Morpho TVL is about $2.1B per the sandbox print.")
