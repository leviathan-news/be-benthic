"""Tests for bot-side build directive execution.

The build directives are emitted by the LLM but executed by benthic-bot.py.
Tests mock subprocess.run so no local or remote build process is started.
Execution is gated on (a) operator sender (enforced at the call site) and
(b) the operator's own message expressing build intent (enforced here).
"""

import importlib.util
import os
import sys
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

BUILD_BIN = str(ROOT / "bin" / "benthic-build")
PM2_BIN = "/opt/node/bin/pm2"
GITHUB_BIN = str(ROOT / "github_client.sh")


def _load_bot_module():
    """Import benthic-bot.py under a Python-safe module name for direct helper tests."""
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


def _ok(stdout='{"task_id": 123}\n'):
    """Return a subprocess.CompletedProcess-like object used by directive tests."""
    return type("RunResult", (), {"returncode": 0, "stdout": stdout, "stderr": ""})()


# A message that expresses build intent (passes the deterministic gate).
_BUILD_MSG = {"message_id": 77, "text": "build me a reg-monitor service"}


def test_build_directive_starts_build_with_multiline_brief_on_stdin():
    bot = _load_bot_module()
    text = (
        "queued\n"
        "[BUILD:reg-monitor]\n"
        "Build an autonomous regulatory monitor.\n"
        "Acceptance: tests pass.\n"
        "[/BUILD]\n"
        "done"
    )
    route_calls = []

    with patch.object(bot, "_write_build_route", side_effect=lambda *args: route_calls.append(args)), \
            patch.object(bot.subprocess, "run", return_value=_ok()) as run:
        cleaned = bot._process_build_directives(
            text, msg_chat_id=-100, msg=_BUILD_MSG, sender={"id": 111000111})

    assert cleaned == "queued\ndone"
    assert route_calls == [(-100, 77, 111000111)]
    run.assert_called_once()
    args, kwargs = run.call_args
    assert args[0] == [BUILD_BIN, "start", "reg-monitor", "--notes", "via chat"]
    assert kwargs["input"] == "Build an autonomous regulatory monitor.\nAcceptance: tests pass."
    assert kwargs["text"] is True
    assert kwargs["capture_output"] is True
    assert "shell" not in kwargs


def test_build_directive_NOT_executed_without_operator_build_intent():
    """The deterministic gate: a build directive whose triggering operator message
    expresses NO build intent must be stripped WITHOUT executing (injection defense)."""
    bot = _load_bot_module()
    text = "sure\n[BUILD:reg-monitor]\nbrief\n[/BUILD]\nok"
    no_intent_msg = {"message_id": 77, "text": "what do you think of this article?"}

    with patch.object(bot, "_write_build_route") as write_route, \
            patch.object(bot.subprocess, "run") as run:
        cleaned = bot._process_build_directives(
            text, msg_chat_id=-100, msg=no_intent_msg, sender={"id": 111000111})

    assert "[BUILD" not in cleaned and "[/BUILD]" not in cleaned  # stripped
    write_route.assert_not_called()
    run.assert_not_called()


def test_build_directive_executes_on_bare_confirmation():
    """A short confirmation ('yes') also passes the gate (operator approving a proposal)."""
    bot = _load_bot_module()
    text = "[BUILD:reg-monitor]\nbrief\n[/BUILD]"

    with patch.object(bot, "_write_build_route"), \
            patch.object(bot.subprocess, "run", return_value=_ok()) as run:
        bot._process_build_directives(
            text, msg_chat_id=-100, msg={"message_id": 1, "text": "yes"},
            sender={"id": 111000111})

    run.assert_called_once()


def test_manage_intent_does_NOT_authorize_a_start():
    """[Important] fix: incidental manage words like 'status' must NOT authorize a
    high-stakes START (per-type gate; START needs explicit build-creation intent)."""
    bot = _load_bot_module()
    text = "[BUILD:reg-monitor]\nbrief\n[/BUILD]"
    msg = {"message_id": 1, "text": "what's the status of ETH today?"}

    with patch.object(bot, "_write_build_route") as write_route, \
            patch.object(bot.subprocess, "run") as run:
        bot._process_build_directives(
            text, msg_chat_id=-1, msg=msg, sender={"id": 111000111})

    run.assert_not_called()
    write_route.assert_not_called()


def test_invalid_build_repo_name_is_rejected_without_subprocess():
    bot = _load_bot_module()
    text = "before\n[BUILD:Bad_Repo]\nbrief\n[/BUILD]\nafter"

    with patch.object(bot, "_write_build_route") as write_route, \
            patch.object(bot.subprocess, "run") as run:
        cleaned = bot._process_build_directives(
            text, msg_chat_id=-100, msg=_BUILD_MSG, sender={"id": 111000111})

    assert cleaned == "before\nafter"
    write_route.assert_not_called()
    run.assert_not_called()


def test_cancel_and_status_directives_invoke_benthic_build():
    bot = _load_bot_module()
    text = "checking\n[BUILD-CANCEL:123]\n[BUILD-STATUS:456]\nclear"
    cancel_msg = {"message_id": 5, "text": "cancel that build and check status"}

    with patch.object(bot.subprocess, "run", return_value=_ok()) as run:
        cleaned = bot._process_build_directives(
            text, msg_chat_id=-100, msg=cancel_msg, sender={"id": 111000111})

    assert cleaned == "checking\nclear"
    calls = [call.args[0] for call in run.call_args_list]
    assert calls == [
        [BUILD_BIN, "cancel", "123"],
        [BUILD_BIN, "status", "456"],
    ]


def test_non_numeric_task_id_is_rejected_without_subprocess():
    """task IDs are integer rowids; --help / task_123 etc. must not spawn a subprocess."""
    bot = _load_bot_module()
    text = "[BUILD-CANCEL:--help]\n[BUILD-STATUS:task_9]\n"
    cancel_msg = {"message_id": 5, "text": "cancel build"}

    with patch.object(bot.subprocess, "run") as run:
        cleaned = bot._process_build_directives(
            text, msg_chat_id=-100, msg=cancel_msg, sender={"id": 111000111})

    run.assert_not_called()
    assert "[BUILD-CANCEL" not in cleaned and "[BUILD-STATUS" not in cleaned


def test_strip_build_directives_removes_all_directive_forms():
    bot = _load_bot_module()
    text = (
        "visible\n[BUILD:thing]\nsecret brief\n[/BUILD]\n"
        "[BUILD-CANCEL:1]\n[BUILD-STATUS:2]\ntail"
    )
    assert bot._strip_build_directives(text) == "visible\ntail"


def test_strip_leaves_no_fragment_from_nested_blocks():
    """Nested/malformed blocks must not leak a stray [/BUILD] or [BUILD:...] fragment."""
    bot = _load_bot_module()
    text = "a [BUILD:outer] x [BUILD:inner] y [/BUILD] z [/BUILD] b"
    cleaned = bot._strip_build_directives(text)
    assert "[BUILD" not in cleaned and "[/BUILD]" not in cleaned


def test_pm2_logs_directive_runs_with_valid_proc_and_default_lines():
    bot = _load_bot_module()
    text = "checking\n[PM2-LOGS:ln-agent]\n"
    msg = {"message_id": 8, "text": "check pm2 logs for the agent"}

    with patch.object(bot, "_PM2_BIN", PM2_BIN), \
            patch.object(bot.subprocess, "run", return_value=_ok("log one\nlog two\n")) as run:
        cleaned = bot._process_pm2_directives(text, msg)

    assert cleaned == "checking\n\nlog one\nlog two"
    run.assert_called_once()
    args, kwargs = run.call_args
    assert args[0] == [PM2_BIN, "logs", "ln-agent", "--nostream", "--lines", "40"]
    assert kwargs["text"] is True
    assert kwargs["capture_output"] is True
    assert "shell" not in kwargs


def test_pm2_logs_directive_caps_numeric_lines():
    bot = _load_bot_module()
    msg = {"message_id": 8, "text": "diagnose pm2 crash logs"}

    with patch.object(bot, "_PM2_BIN", PM2_BIN), \
            patch.object(bot.subprocess, "run", return_value=_ok("logs\n")) as run:
        bot._process_pm2_directives("[PM2-LOGS:benthic-bot 999]", msg)

    args, _ = run.call_args
    assert args[0] == [PM2_BIN, "logs", "benthic-bot", "--nostream", "--lines", "200"]


def test_pm2_list_and_show_directives_run_with_valid_process():
    bot = _load_bot_module()
    text = "[PM2-LIST]\n[PM2-SHOW:benthic-builder]\n"
    msg = {"message_id": 8, "text": "pm2 process status"}

    with patch.object(bot, "_PM2_BIN", PM2_BIN), \
            patch.object(bot.subprocess, "run", return_value=_ok("ok\n")) as run:
        cleaned = bot._process_pm2_directives(text, msg)

    assert cleaned == "ok\n\nok"
    calls = [call.args[0] for call in run.call_args_list]
    assert calls == [
        [PM2_BIN, "list"],
        [PM2_BIN, "show", "benthic-builder"],
    ]


def test_pm2_directive_rejects_bad_proc_and_non_numeric_lines():
    bot = _load_bot_module()
    text = "[PM2-LOGS:../../secrets 10]\n[PM2-LOGS:ln-agent abc]\n[PM2-SHOW:bad-proc]\n"
    msg = {"message_id": 8, "text": "check pm2 logs"}

    with patch.object(bot.subprocess, "run") as run:
        cleaned = bot._process_pm2_directives(text, msg)

    assert cleaned == ""
    run.assert_not_called()


def test_pm2_directive_without_diagnostics_intent_is_stripped_not_executed():
    bot = _load_bot_module()
    text = "sure\n[PM2-LIST]\n"
    msg = {"message_id": 8, "text": "what do you think of this article?"}

    with patch.object(bot.subprocess, "run") as run:
        cleaned = bot._process_pm2_directives(text, msg)

    assert cleaned == "sure"
    run.assert_not_called()


def test_strip_pm2_directives_removes_all_directive_forms_without_executing():
    bot = _load_bot_module()
    text = "visible\n[PM2-LOGS:ln-agent 20]\n[PM2-LIST]\n[PM2-SHOW:benthic-bot]\ntail"

    with patch.object(bot.subprocess, "run") as run:
        cleaned = bot._strip_pm2_directives(text)

    assert cleaned == "visible\ntail"
    run.assert_not_called()


def test_pm2_output_is_sent_via_telegram_chunks():
    bot = _load_bot_module()
    long_output = "x" * 5000
    msg = {"message_id": 8, "text": "pm2 list please"}

    with patch.object(bot, "_PM2_BIN", PM2_BIN), \
            patch.object(bot.subprocess, "run", return_value=_ok(long_output)):
        response = bot._process_pm2_directives("[PM2-LIST]", msg)

    sent = []
    with patch.object(bot, "tg_request", side_effect=lambda method, data: sent.append(data) or {"ok": True}), \
            patch.object(bot.time, "sleep"):
        bot.send_message(123, response)

    assert len(sent) == 2
    assert "".join(chunk["text"] for chunk in sent) == long_output
    assert all(len(chunk["text"]) <= 4096 for chunk in sent)


def test_github_issue_create_directive_runs_operator_client():
    bot = _load_bot_module()
    text = "filing\n[GH:issue create leviathan-news/be-benthic || Bug title || Body line 1\nBody line 2]\n"
    msg = {"message_id": 9, "text": "open an issue for this bug"}

    with patch.object(bot, "_GITHUB_CLIENT_BIN", GITHUB_BIN), \
            patch.object(bot.subprocess, "run", return_value=_ok("https://github.com/leviathan-news/be-benthic/issues/7\n")) as run:
        cleaned = bot._process_github_directives(text, msg)

    assert cleaned == "filing"
    args, kwargs = run.call_args
    assert args[0] == [
        GITHUB_BIN, "--operator", "issue", "create", "leviathan-news/be-benthic",
        "--title", "Bug title", "--body", "Body line 1\nBody line 2",
    ]
    assert kwargs["text"] is True
    assert kwargs["capture_output"] is True
    assert "shell" not in kwargs


def test_github_issue_comment_and_pr_comment_directives_run_operator_client():
    bot = _load_bot_module()
    text = (
        "[GH:issue comment leviathan-news/be-benthic 42 || issue body]\n"
        "[GH:pr comment leviathan-news/be-benthic 43 || pr body]\n"
    )
    msg = {"message_id": 9, "text": "comment on the github issue and pr"}

    with patch.object(bot, "_GITHUB_CLIENT_BIN", GITHUB_BIN), \
            patch.object(bot.subprocess, "run", return_value=_ok("ok\n")) as run:
        cleaned = bot._process_github_directives(text, msg)

    assert cleaned == ""
    calls = [call.args[0] for call in run.call_args_list]
    assert calls == [
        [GITHUB_BIN, "--operator", "issue", "comment", "leviathan-news/be-benthic", "42", "--body", "issue body"],
        [GITHUB_BIN, "--operator", "pr", "comment", "leviathan-news/be-benthic", "43", "--body", "pr body"],
    ]


def test_github_pr_create_directive_runs_operator_client():
    bot = _load_bot_module()
    text = "[GH:pr create leviathan-news/be-benthic || PR title || PR body || feat/operator-directives || main]"
    msg = {"message_id": 9, "text": "open a pull request for this branch"}

    with patch.object(bot, "_GITHUB_CLIENT_BIN", GITHUB_BIN), \
            patch.object(bot.subprocess, "run", return_value=_ok("https://github.com/pr\n")) as run:
        bot._process_github_directives(text, msg)

    args, _ = run.call_args
    assert args[0] == [
        GITHUB_BIN, "--operator", "pr", "create", "leviathan-news/be-benthic",
        "--title", "PR title", "--body", "PR body",
        "--head", "feat/operator-directives", "--base", "main",
    ]


def test_github_directive_rejects_bad_repo_and_non_numeric_number():
    bot = _load_bot_module()
    text = (
        "[GH:issue create bad repo || title || body]\n"
        "[GH:issue comment leviathan-news/be-benthic abc || body]\n"
        "[GH:pr comment bad/repo/extra 12 || body]\n"
    )
    msg = {"message_id": 9, "text": "github issue comment"}

    with patch.object(bot.subprocess, "run") as run:
        cleaned = bot._process_github_directives(text, msg)

    assert cleaned == ""
    run.assert_not_called()


def test_github_directive_without_github_intent_is_stripped_not_executed():
    bot = _load_bot_module()
    text = "looks good\n[GH:issue create leviathan-news/be-benthic || title || body]\n"
    msg = {"message_id": 9, "text": "what do you think of this article?"}

    with patch.object(bot.subprocess, "run") as run:
        cleaned = bot._process_github_directives(text, msg)

    assert cleaned == "looks good"
    run.assert_not_called()


def test_strip_github_directives_removes_directives_without_executing():
    bot = _load_bot_module()
    text = (
        "visible\n"
        "[GH:issue create leviathan-news/be-benthic || title || body]\n"
        "[GH:pr comment leviathan-news/be-benthic 7 || body]\n"
        "tail"
    )

    with patch.object(bot.subprocess, "run") as run:
        cleaned = bot._strip_github_directives(text)

    assert cleaned == "visible\ntail"
    run.assert_not_called()


def test_apply_operator_directives_derives_chat_id_no_nameerror():
    """Regression: the operator-directive wiring derives msg_chat_id itself, so the
    poll path can't NameError on it (that exact bug shipped once via inline wiring)."""
    bot = _load_bot_module()
    text = "[BUILD:reg-monitor]\nbrief\n[/BUILD]"
    msg = {"message_id": 9, "chat": {"id": -100123}, "text": "build me a reg-monitor"}
    with patch.object(bot, "_write_build_route") as write_route, \
            patch.object(bot.subprocess, "run", return_value=_ok()) as run:
        bot._apply_operator_directives(text, msg, {"id": 111000111})  # must not raise NameError
    write_route.assert_called_once_with(-100123, 9, 111000111)
    run.assert_called_once()


def test_strip_all_directives_covers_every_family():
    bot = _load_bot_module()
    text = ("hi\n[BUILD:x]\nb\n[/BUILD]\n[PM2-LIST]\n"
            "[GH:issue create o/r || t || b]\n[REMEMBER:note] x\ntail")
    out = bot._strip_all_directives(text)
    for tok in ("[BUILD", "[/BUILD]", "[PM2", "[GH:", "[REMEMBER"):
        assert tok not in out
    assert "hi" in out and "tail" in out
