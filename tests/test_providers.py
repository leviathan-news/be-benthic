"""Tests for the provider-agnostic LLM dispatch layer."""

import os
import logging
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest
import providers

sys.path.insert(0, str(Path(__file__).parent.parent))

from providers import (
    CircuitBreaker,
    ClaudeProvider,
    CodexProvider,
    OpenCodeProvider,
    ProviderCall,
    ProviderChain,
    ProviderResult,
    build_provider_env,
    looks_like_claude_limit_error,
    strip_surrogates,
)


# ─── CircuitBreaker ──────────────────────────────────────────────────────────


def test_breaker_available_until_max_failures():
    cb = CircuitBreaker(max_failures=3, name="t")
    assert cb.is_available()
    cb.record_failure()
    cb.record_failure()
    assert cb.is_available()
    cb.record_failure()
    assert not cb.is_available()


def test_breaker_record_success_resets_count():
    cb = CircuitBreaker(max_failures=3, name="t")
    cb.record_failure()
    cb.record_failure()
    cb.record_success()
    assert cb.is_available()
    cb.record_failure()
    cb.record_failure()
    cb.record_failure()
    assert not cb.is_available()


def test_breaker_open_cooldown_blocks_until_elapsed():
    cb = CircuitBreaker(max_failures=3, name="t")
    cb.open_cooldown(cooldown=60, reason="quota")
    assert not cb.is_available()
    assert cb.cooldown_remaining() > 0


def test_breaker_reset_failures_keeps_cooldown():
    """reset_failures clears transient errors but does NOT clear the quota cooldown."""
    cb = CircuitBreaker(max_failures=3, name="t")
    cb.open_cooldown(cooldown=60, reason="quota")
    cb.reset_failures()
    # Still unavailable because cooldown is independent of the failure count
    assert not cb.is_available()


# ─── Helpers ────────────────────────────────────────────────────────────────


def test_build_provider_env_includes_bin_parent():
    env = build_provider_env("/usr/local/bin/claude")
    assert "/usr/local/bin" in env["PATH"]


def test_build_provider_env_merges_extra_env_without_replacing_base():
    """Per-call routing variables are added on top of the normal provider env."""
    env = build_provider_env(
        "/usr/local/bin/codex",
        extra_env={"BENTHIC_BUILD_CHAT": "-999"},
    )

    assert env["BENTHIC_BUILD_CHAT"] == "-999"
    assert "PATH" in env
    assert "/usr/local/bin" in env["PATH"]


def test_build_provider_env_strips_secret_vars_for_codex():
    """A shell-capable Codex must not inherit any reusable API credential."""
    with patch.dict(os.environ,
                    {"SOME_TOKEN": "x", "MY_SECRET": "y", "DB_PASSWORD": "z",
                     "ETHERSCAN_API_KEY": "keep", "PROVIDER_ORDER": "codex"},
                    clear=False):
        env = build_provider_env("/usr/local/bin/codex", strip_secret_vars=True)
    assert "SOME_TOKEN" not in env
    assert "MY_SECRET" not in env
    assert "DB_PASSWORD" not in env
    assert "ETHERSCAN_API_KEY" not in env
    assert env.get("PROVIDER_ORDER") == "codex"      # non-secret var survives
    assert "PATH" in env


def test_build_provider_env_keeps_non_etherscan_vars_when_not_stripping():
    """Claude keeps its auth environment but never receives Etherscan access."""
    with patch.dict(
            os.environ,
            {"SOME_TOKEN": "x", "ETHERSCAN_API_KEY": "inherited"},
            clear=False):
        env = build_provider_env(
            "/usr/local/bin/claude",
            extra_env={
                "ANTHROPIC_API_KEY": "claude-auth",
                "ETHERSCAN_API_KEY": "reintroduced",
            },
        )

    assert env.get("SOME_TOKEN") == "x"
    assert env.get("ANTHROPIC_API_KEY") == "claude-auth"
    assert "ETHERSCAN_API_KEY" not in env


def test_strip_surrogates_removes_unpaired():
    s = "hello \udcc4 world"
    out = strip_surrogates(s)
    assert "\udcc4" not in out


def test_looks_like_claude_limit_error_detects_quota():
    assert looks_like_claude_limit_error("", "You hit your limit")
    assert looks_like_claude_limit_error("Error: rate limit exceeded", "")
    assert looks_like_claude_limit_error("status code 501", "")
    assert not looks_like_claude_limit_error("normal response", "")


# ─── ClaudeProvider ─────────────────────────────────────────────────────────


def test_claude_returns_empty_on_timeout():
    p = ClaudeProvider(bin="/bin/echo", retries=0)
    with patch("subprocess.run",
               side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=1)):
        result = p.ask("test", timeout=1)
    assert result == ""


def test_claude_returns_empty_on_nonzero_exit():
    p = ClaudeProvider(bin="/bin/echo", retries=0)
    mock_result = type("R", (), {"returncode": 1, "stdout": "", "stderr": "error"})()
    with patch("subprocess.run", return_value=mock_result):
        result = p.ask("test", timeout=1)
    assert result == ""


def test_claude_subprocess_never_receives_etherscan_from_parent_or_extra_env():
    """The subprocess boundary removes Etherscan after call-scoped env merging."""
    provider = ClaudeProvider(bin="/bin/echo", retries=0)
    completed = type(
        "R", (), {"returncode": 0, "stdout": "answer", "stderr": ""})()

    with patch.dict(
            os.environ, {"ETHERSCAN_API_KEY": "inherited"}, clear=False):
        with patch("subprocess.run", return_value=completed) as run:
            result = provider.ask(
                "test",
                timeout=1,
                extra_env={
                    "ANTHROPIC_API_KEY": "claude-auth",
                    "ETHERSCAN_API_KEY": "reintroduced",
                },
            )

    assert result == "answer"
    subprocess_env = run.call_args.kwargs["env"]
    assert subprocess_env.get("ANTHROPIC_API_KEY") == "claude-auth"
    assert "ETHERSCAN_API_KEY" not in subprocess_env


def test_claude_detects_quota_error_and_opens_cooldown():
    p = ClaudeProvider(bin="/bin/echo", retries=0, quota_cooldown=3600)
    mock_result = type("R", (), {"returncode": 1, "stdout": "", "stderr": "rate limit"})()
    with patch("subprocess.run", return_value=mock_result):
        result = p.ask("test", timeout=1)
    assert result == ""
    # After a quota error, cooldown is open and breaker reports unavailable
    assert not p.is_available()
    assert p.breaker.cooldown_remaining() > 0


def test_claude_detects_soft_error_with_zero_exit():
    """Returncode 0 but stdout starts with 'Error:' is still a failure."""
    p = ClaudeProvider(bin="/bin/echo", retries=0)
    mock_result = type("R", (), {"returncode": 0, "stdout": "Error: something broke", "stderr": ""})()
    with patch("subprocess.run", return_value=mock_result):
        result = p.ask("test", timeout=1)
    assert result == ""


def test_claude_success_returns_stdout_and_resets_breaker():
    p = ClaudeProvider(bin="/bin/echo", retries=0)
    p.breaker.record_failure()
    p.breaker.record_failure()
    mock_result = type("R", (), {"returncode": 0, "stdout": "hello world", "stderr": ""})()
    with patch("subprocess.run", return_value=mock_result):
        result = p.ask("test", timeout=1)
    assert result == "hello world"
    # Success resets the consecutive-failure count
    assert p.is_available()


def test_claude_wrapper_transforms_prompt_input():
    """ClaudeProvider wrappers add component-specific output discipline."""
    captured = []

    def fake_run(*args, **kwargs):
        captured.append(kwargs.get("input"))
        return type("R", (), {"returncode": 0, "stdout": "ok", "stderr": ""})()

    p = ClaudeProvider(
        bin="/bin/echo",
        retries=0,
        wrapper=lambda prompt: f"WRAPPED:\n{prompt}",
    )
    with patch("subprocess.run", side_effect=fake_run):
        assert p.ask("original prompt", timeout=1) == "ok"

    assert captured == ["WRAPPED:\noriginal prompt"]


# ─── CodexProvider ──────────────────────────────────────────────────────────


def test_codex_returns_empty_on_failure():
    p = CodexProvider(bin="/bin/echo")
    mock_result = type("R", (), {"returncode": 1, "stdout": "", "stderr": "codex error"})()
    with patch("subprocess.run", return_value=mock_result):
        result = p.ask("test", timeout=1)
    assert result == ""


def test_codex_returns_empty_on_timeout():
    p = CodexProvider(bin="/bin/echo")
    with patch("subprocess.run",
               side_effect=subprocess.TimeoutExpired(cmd="codex", timeout=1)):
        result = p.ask("test", timeout=1)
    assert result == ""


def test_codex_failure_records_breaker():
    p = CodexProvider(bin="/bin/echo")
    mock_result = type("R", (), {"returncode": 1, "stdout": "", "stderr": "err"})()
    with patch("subprocess.run", return_value=mock_result):
        for _ in range(3):
            p.ask("test", timeout=1)
    assert not p.is_available()


def test_codex_permission_profile_from_constructor_locks_down_sandbox_bypass():
    """A configured profile selects Codex permissions and omits sandbox bypass."""
    p = CodexProvider(bin="/bin/echo", permission_profile="benthic_agent")
    calls, fake = _captured_subprocess_args()
    with patch("subprocess.run", side_effect=fake):
        p.ask("test", timeout=1)
    cmd = calls[0]
    assert "default_permissions=benthic_agent" in cmd
    assert "approval_policy=never" in cmd
    assert "--dangerously-bypass-approvals-and-sandbox" not in cmd


def test_codex_permission_profile_from_per_call_kwarg_locks_down_sandbox_bypass():
    """A per-call profile selects Codex permissions and omits sandbox bypass."""
    p = CodexProvider(bin="/bin/echo")
    calls, fake = _captured_subprocess_args()
    with patch("subprocess.run", side_effect=fake):
        p.ask("test", timeout=1, permission_profile="benthic_bot")
    cmd = calls[0]
    assert "default_permissions=benthic_bot" in cmd
    assert "approval_policy=never" in cmd
    assert "--dangerously-bypass-approvals-and-sandbox" not in cmd


def test_codex_permission_profile_none_keeps_legacy_sandbox_bypass():
    """No profile preserves the historical bypass flag for back-compat."""
    p = CodexProvider(bin="/bin/echo", permission_profile=None)
    calls, fake = _captured_subprocess_args()
    with patch("subprocess.run", side_effect=fake):
        p.ask("test", timeout=1)
    cmd = calls[0]
    assert "--dangerously-bypass-approvals-and-sandbox" in cmd
    assert not any("default_permissions=" in c for c in cmd)


def test_codex_profile_suppresses_add_dirs():
    """Permission profiles do NOT compose with --add-dir; it must be omitted
    when a profile is active (the combo errors in codex)."""
    p = CodexProvider(bin="/bin/echo", permission_profile="benthic_agent",
                      add_dirs=["~/.claude"])
    calls, fake = _captured_subprocess_args()
    with patch("subprocess.run", side_effect=fake):
        p.ask("test", timeout=1)
    cmd = calls[0]
    assert "--add-dir" not in cmd
    assert "default_permissions=benthic_agent" in cmd


def test_codex_bypass_mode_keeps_add_dirs():
    """With no profile (bypass/back-compat), configured add_dirs are still emitted."""
    p = CodexProvider(bin="/bin/echo", add_dirs=["~/.claude"])
    calls, fake = _captured_subprocess_args()
    with patch("subprocess.run", side_effect=fake):
        p.ask("test", timeout=1)
    cmd = calls[0]
    assert "--add-dir" in cmd


def test_codex_blank_per_call_profile_falls_back_to_constructor():
    """A blank per-call profile must use the constructor profile, NOT drop to
    full sandbox bypass (the empty-string footgun)."""
    p = CodexProvider(bin="/bin/echo", permission_profile="benthic_bot")
    calls, fake = _captured_subprocess_args()
    with patch("subprocess.run", side_effect=fake):
        p.ask("test", timeout=1, permission_profile="")
    cmd = calls[0]
    assert "default_permissions=benthic_bot" in cmd
    assert "--dangerously-bypass-approvals-and-sandbox" not in cmd


def test_codex_per_call_permission_profile_overrides_constructor_default():
    """The call-scoped profile wins over the provider construction default."""
    p = CodexProvider(bin="/bin/echo", permission_profile="benthic_agent")
    calls, fake = _captured_subprocess_args()
    with patch("subprocess.run", side_effect=fake):
        p.ask("test", timeout=1, permission_profile="benthic_bot_operator")
    cmd = calls[0]
    assert "default_permissions=benthic_bot_operator" in cmd
    assert "default_permissions=benthic_agent" not in cmd
    assert "approval_policy=never" in cmd
    assert "--dangerously-bypass-approvals-and-sandbox" not in cmd


# ─── OpenCodeProvider ───────────────────────────────────────────────────────


def test_opencode_unavailable_when_not_configured():
    """An OpenCode provider with an empty model is always unavailable."""
    p = OpenCodeProvider(bin="/bin/echo", model="")
    assert not p.is_available()
    assert p.ask("test", timeout=1) == ""


def test_opencode_available_when_configured():
    p = OpenCodeProvider(bin="/bin/echo", model="some/model")
    assert p.is_available()


# ─── ProviderChain ──────────────────────────────────────────────────────────


class ReceiptProvider:
    def __init__(self, name, answers, model="model", effort="low"):
        self.name = name
        self.answers = iter(answers)
        self.tiers = {}
        self.breaker = CircuitBreaker(max_failures=3, name=name)
        self.call = ProviderCall(model=model, effort=effort, tier=None)

    def is_available(self):
        return self.breaker.is_available()

    def ask(self, prompt, *, timeout=3600, **kwargs):
        return next(self.answers)

    def resolved_call(self, **kwargs):
        return self.call


def test_receipt_identifies_actual_fallback():
    chain = ProviderChain([
        ReceiptProvider("primary", [""]),
        ReceiptProvider("secondary", ["answer"], "secondary-model", "high"),
    ])
    result = chain.ask_with_receipt("prompt", timeout=1)
    assert result == ProviderResult(
        "answer", "secondary", "secondary-model", "high", None
    )


def test_validated_call_rejects_nonempty_invalid_primary():
    primary = ReceiptProvider("primary", ["not-json"])
    secondary = ReceiptProvider("secondary", ['{"pass":true}'])
    chain = ProviderChain([primary, secondary])
    result = chain.ask_validated(
        "prompt", validator=lambda text: text.startswith("{"), timeout=1
    )
    assert result.provider == "secondary"
    assert primary.breaker._failures == 1


def test_validated_call_uses_at_most_one_fallback():
    providers = [
        ReceiptProvider("first", ["bad"]),
        ReceiptProvider("second", ["still-bad"]),
        ReceiptProvider("third", ['{"ok":true}']),
    ]
    chain = ProviderChain(providers)
    result = chain.ask_validated(
        "prompt", validator=lambda text: text.startswith("{"), timeout=1
    )
    assert result is None
    assert next(providers[2].answers) == '{"ok":true}'


def test_validated_concrete_provider_preserves_failures_on_rejection():
    provider = ClaudeProvider(bin="/bin/echo", retries=0)
    provider.breaker.record_failure()
    provider.breaker.record_failure()
    chain = ProviderChain([provider])
    invalid = type("R", (), {
        "returncode": 0,
        "stdout": "not-json",
        "stderr": "",
    })()

    with patch("subprocess.run", return_value=invalid):
        result = chain.ask_validated(
            "prompt", validator=lambda text: text.startswith("{"), timeout=1
        )

    assert result is None
    assert provider.breaker._failures == 3
    assert not provider.is_available()


def test_validated_concrete_provider_clears_failures_after_validation():
    provider = ClaudeProvider(bin="/bin/echo", retries=0)
    provider.breaker.record_failure()
    provider.breaker.record_failure()
    chain = ProviderChain([provider])
    valid = type("R", (), {
        "returncode": 0,
        "stdout": '{"ok":true}',
        "stderr": "",
    })()
    failures_during_validation = []

    def validator(text):
        failures_during_validation.append(provider.breaker._failures)
        return text.startswith("{")

    with patch("subprocess.run", return_value=valid):
        result = chain.ask_validated("prompt", validator=validator, timeout=1)

    assert result.provider == "claude"
    assert failures_during_validation == [2]
    assert provider.breaker._failures == 0


def test_receipts_are_not_shared_mutable_state():
    first = ProviderChain([ReceiptProvider("a", ["first"], "m1", "low")])
    second = ProviderChain([ReceiptProvider("b", ["second"], "m2", "high")])
    assert first.ask_with_receipt("x").provider == "a"
    assert second.ask_with_receipt("y").provider == "b"


def test_provider_chain_absolute_deadline_caps_each_fallback_timeout(
        monkeypatch):
    """Each fallback receives only the remaining absolute wall-clock budget."""
    clock = [0.0]
    timeouts = []

    class DeadlineProvider(ReceiptProvider):
        def ask(self, prompt, *, timeout=3600, **kwargs):
            del prompt, kwargs
            timeouts.append(timeout)
            clock[0] += 2.0
            return next(self.answers)

    monkeypatch.setattr(providers.time, "monotonic", lambda: clock[0])
    chain = ProviderChain([
        DeadlineProvider("primary", [""]),
        DeadlineProvider("fallback", ['{"ok":true}']),
    ])

    result = chain.ask_validated(
        "prompt",
        validator=lambda text: text.startswith("{"),
        timeout=20,
        deadline=5.0,
    )

    assert result.provider == "fallback"
    assert timeouts == [5.0, 3.0]


def test_provider_chain_absolute_deadline_discards_late_success(monkeypatch):
    """A provider that returns after the deadline cannot publish its receipt."""
    clock = [0.0]

    class LateProvider(ReceiptProvider):
        def ask(self, prompt, *, timeout=3600, **kwargs):
            del prompt, timeout, kwargs
            clock[0] = 6.0
            return next(self.answers)

    monkeypatch.setattr(providers.time, "monotonic", lambda: clock[0])
    chain = ProviderChain([LateProvider("late", ['{"ok":true}'])])

    assert chain.ask_validated(
        "prompt",
        validator=lambda text: text.startswith("{"),
        timeout=20,
        deadline=5.0,
    ) is None


def test_provider_chain_absolute_deadline_rejects_late_validator(monkeypatch):
    """Contract validation cannot accept output after the absolute deadline."""
    clock = [0.0]
    provider = ReceiptProvider("primary", ['{"ok":true}'])

    def late_validator(text):
        assert text == '{"ok":true}'
        clock[0] = 6.0
        return True

    monkeypatch.setattr(providers.time, "monotonic", lambda: clock[0])

    assert ProviderChain([provider]).ask_validated(
        "prompt",
        validator=late_validator,
        timeout=20,
        deadline=5.0,
    ) is None
    assert provider.breaker._failures == 1


def test_claude_absolute_deadline_prevents_retry_and_fallback(monkeypatch):
    """A failed Claude attempt that consumes the deadline ends the whole chain."""
    clock = [0.0]
    subprocess_timeouts = []
    retry_sleeps = []
    fallback_calls = []

    def failed_run(*args, **kwargs):
        del args
        subprocess_timeouts.append(kwargs["timeout"])
        clock[0] = 6.0
        return type("R", (), {
            "returncode": 1,
            "stdout": "",
            "stderr": "transient failure",
        })()

    class FallbackProvider(ReceiptProvider):
        def ask(self, prompt, *, timeout=3600, **kwargs):
            fallback_calls.append((prompt, timeout, kwargs))
            return next(self.answers)

    monkeypatch.setattr(providers.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(providers.time, "sleep", retry_sleeps.append)
    monkeypatch.setattr(providers.subprocess, "run", failed_run)
    chain = ProviderChain([
        ClaudeProvider(bin="/bin/echo", retries=2),
        FallbackProvider("fallback", ['{"ok":true}']),
    ])

    result = chain.ask_validated(
        "prompt",
        validator=lambda text: text.startswith("{"),
        timeout=300,
        deadline=5.0,
    )

    assert result is None
    assert subprocess_timeouts == [5.0]
    assert retry_sleeps == []
    assert fallback_calls == []


@pytest.mark.parametrize(
    ("provider", "tools"),
    (
        (CodexProvider(bin="/bin/echo"), "WebSearch"),
        (OpenCodeProvider(bin="/bin/echo", model="openai/model"), None),
    ),
)
def test_concrete_provider_subprocess_honors_absolute_deadline(
        monkeypatch, provider, tools):
    """Codex and OpenCode cannot expand a caller's absolute timeout."""
    subprocess_timeouts = []

    def successful_run(*args, **kwargs):
        del args
        subprocess_timeouts.append(kwargs["timeout"])
        return type("R", (), {
            "returncode": 0,
            "stdout": "answer",
            "stderr": "",
        })()

    monkeypatch.setattr(providers.time, "monotonic", lambda: 2.0)
    monkeypatch.setattr(providers.subprocess, "run", successful_run)

    assert provider.ask(
        "prompt",
        timeout=300,
        tools=tools,
        _deadline=5.0,
    ) == "answer"
    assert subprocess_timeouts == [3.0]


def _assert_claude_hard_stage_flags(command):
    """Assert the common non-interactive confinement flags for hard stages."""
    assert "--safe-mode" in command
    assert command[command.index("--permission-mode") + 1] == "dontAsk"
    assert "--no-session-persistence" in command
    assert "--disable-slash-commands" in command
    assert "--bare" not in command


def test_claude_media_sentinel_scopes_read_to_exact_allowed_paths(tmp_path):
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    first.write_bytes(b"PNG1")
    second.write_bytes(b"PNG2")
    allowed_paths = (str(first.resolve()), str(second.resolve()))
    provider = ClaudeProvider(bin="/bin/echo", retries=0)
    calls, fake = _captured_subprocess_args()
    with patch("subprocess.run", side_effect=fake):
        provider.ask(
            "inspect image",
            timeout=1,
            tools="__media__",
            allowed_paths=allowed_paths,
        )
    command = calls[0]
    _assert_claude_hard_stage_flags(command)
    assert command[command.index("--tools") + 1] == "Read"
    allowed_rules = command[command.index("--allowedTools") + 1]
    assert allowed_rules == ",".join(
        f"Read({path})" for path in allowed_paths
    )
    assert allowed_rules != "Read"
    assert "WebSearch" not in allowed_rules
    assert "WebFetch" not in allowed_rules
    assert "/unlisted/secret" not in allowed_rules


def test_claude_media_rejects_missing_mutable_or_unsafe_allowlists_without_spawn(
        tmp_path):
    regular = tmp_path / "regular.png"
    regular.write_bytes(b"PNG")
    directory = tmp_path / "directory"
    directory.mkdir()
    symlink = tmp_path / "linked.png"
    symlink.symlink_to(regular)
    comma = tmp_path / "bad,name.png"
    comma.write_bytes(b"PNG")
    parenthesis = tmp_path / "bad).png"
    parenthesis.write_bytes(b"PNG")
    wildcard = tmp_path / "bad*.png"
    wildcard.write_bytes(b"PNG")
    control = tmp_path / "bad\nname.png"
    control.write_bytes(b"PNG")
    invalid_allowlists = (
        None,
        (),
        [str(regular.resolve())],
        (str(tmp_path / "missing.png"),),
        (str(directory.resolve()),),
        (str(symlink),),
        (str(comma.resolve()),),
        (str(parenthesis.resolve()),),
        (str(wildcard.resolve()),),
        (str(control.resolve()),),
        ("relative.png",),
    )
    provider = ClaudeProvider(bin="/bin/echo", retries=0)

    with patch("subprocess.run") as run:
        for allowed_paths in invalid_allowlists:
            assert provider.ask(
                "inspect image",
                timeout=1,
                tools="__media__",
                allowed_paths=allowed_paths,
            ) == ""

    run.assert_not_called()


def test_codex_media_sentinel_attaches_only_exact_allowed_paths(tmp_path):
    """Codex receives selected images as attachments, not a filesystem image tool."""
    captured = {}

    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    first.write_bytes(b"PNG1")
    second.write_bytes(b"PNG2")
    allowed_paths = (str(first.resolve()), str(second.resolve()))

    def fake_run(*args, **kwargs):
        captured["cmd"] = args[0]
        captured["cwd"] = kwargs["cwd"]
        return type("R", (), {
            "returncode": 0,
            "stdout": "observed",
            "stderr": "",
        })()

    provider = CodexProvider(
        bin="/bin/echo",
        cwd="/srv/ln-agent",
        permission_profile="benthic_bot",
    )
    with patch("subprocess.run", side_effect=fake_run):
        assert provider.ask(
            "inspect selected images",
            timeout=1,
            tools="__media__",
            allowed_paths=allowed_paths,
        ) == "observed"
    command = captured["cmd"]
    assert "--ignore-user-config" in command
    assert 'web_search="disabled"' in command
    assert "tools.view_image=false" in command
    assert [
        command[index + 1]
        for index, value in enumerate(command[:-1])
        if value in {"-i", "--image"}
    ] == list(allowed_paths)
    disabled = {
        command[index + 1]
        for index, value in enumerate(command[:-1])
        if value == "--disable"
    }
    assert {"shell_tool", "browser_use", "apps", "image_generation"} <= disabled
    assert "--dangerously-bypass-approvals-and-sandbox" not in command
    assert not any(
        value.startswith("default_permissions=") for value in command
    )


def test_codex_media_rejects_invalid_allowlists_without_spawn(tmp_path):
    """Every media attachment must be a unique canonical regular file."""
    regular = tmp_path / "regular.png"
    regular.write_bytes(b"PNG")
    directory = tmp_path / "directory"
    directory.mkdir()
    symlink = tmp_path / "linked.png"
    symlink.symlink_to(regular)
    invalid_allowlists = (
        None,
        (),
        [str(regular.resolve())],
        ("relative.png",),
        (str(tmp_path / "missing.png"),),
        (str(directory.resolve()),),
        (str(symlink),),
        (str(regular.resolve()), str(regular.resolve())),
    )
    provider = CodexProvider(bin="/bin/echo")

    with patch("subprocess.run") as run:
        for allowed_paths in invalid_allowlists:
            assert provider.ask(
                "inspect selected images",
                timeout=1,
                tools="__media__",
                allowed_paths=allowed_paths,
            ) == ""

    run.assert_not_called()


def test_opencode_declines_unenforceable_media_sentinel():
    provider = OpenCodeProvider(bin="opencode", model="configured")
    assert provider.ask("inspect", timeout=1, tools="__media__") == ""


def test_claude_research_sentinel_maps_only_to_web_reads():
    provider = ClaudeProvider(bin="/bin/echo", retries=0)
    calls, fake = _captured_subprocess_args()
    with patch("subprocess.run", side_effect=fake):
        provider.ask("find sources", timeout=1, tools="__research__")
    command = calls[0]
    _assert_claude_hard_stage_flags(command)
    assert command[command.index("--tools") + 1] == "WebSearch,WebFetch"
    assert command[command.index("--allowedTools") + 1] == "WebSearch,WebFetch"


def test_claude_none_sentinel_disables_every_tool_and_prompt(tmp_path):
    provider = ClaudeProvider(bin="/bin/echo", retries=0)
    calls, fake = _captured_subprocess_args()
    with patch("subprocess.run", side_effect=fake):
        provider.ask("compose", timeout=1, tools="__none__")
    command = calls[0]
    _assert_claude_hard_stage_flags(command)
    assert command[command.index("--tools") + 1] == ""
    assert command[command.index("--allowedTools") + 1] == ""


def test_claude_ordinary_tool_calls_remain_compatible():
    provider = ClaudeProvider(bin="/bin/echo", retries=0)
    calls, fake = _captured_subprocess_args()
    with patch("subprocess.run", side_effect=fake):
        provider.ask("ordinary", timeout=1, tools="WebSearch,Read")
    command = calls[0]
    assert command[command.index("--allowedTools") + 1] == "WebSearch,Read"
    assert "--tools" not in command
    assert "--safe-mode" not in command
    assert "--permission-mode" not in command


def test_codex_research_sentinel_enables_web_without_shell():
    calls, fake = _captured_subprocess_args()
    provider = CodexProvider(bin="/bin/echo", cwd="/srv/ln-agent")
    with patch("subprocess.run", side_effect=fake):
        provider.ask("find sources", timeout=1, tools="__research__")
    command = calls[0]
    disabled = {
        command[index + 1]
        for index, value in enumerate(command[:-1])
        if value == "--disable"
    }
    assert "tools.web_search=true" in command
    assert "shell_tool" in disabled
    assert "apps" in disabled
    assert "browser_use" in disabled
    assert "tools.view_image=false" in command


def test_opencode_declines_unenforceable_research_sentinel():
    provider = OpenCodeProvider(bin="opencode", model="configured")
    assert provider.ask("find sources", timeout=1, tools="__research__") == ""


def test_chain_returns_first_non_empty():
    """The chain returns whatever the first available provider produces."""
    primary = ClaudeProvider(bin="/bin/echo", retries=0)
    secondary = CodexProvider(bin="/bin/echo")
    chain = ProviderChain([primary, secondary])
    mock_result = type("R", (), {"returncode": 0, "stdout": "primary won", "stderr": ""})()
    with patch("subprocess.run", return_value=mock_result):
        result = chain.ask("test", timeout=1)
    assert result == "primary won"


def test_chain_falls_through_on_empty_primary():
    """When the primary returns empty (e.g. subprocess failed), the next provider runs."""
    primary = ClaudeProvider(bin="/bin/echo", retries=0)
    secondary = CodexProvider(bin="/bin/echo")
    chain = ProviderChain([primary, secondary])

    claude_fail = type("R", (), {"returncode": 1, "stdout": "", "stderr": "fail"})()
    codex_ok = type("R", (), {"returncode": 0, "stdout": "codex won", "stderr": ""})()

    # subprocess.run is called twice: once for Claude (fails), once for Codex (succeeds)
    with patch("subprocess.run", side_effect=[claude_fail, codex_ok]):
        result = chain.ask("test", timeout=1)
    assert result == "codex won"


def test_chain_logs_provider_that_answers_after_fallback(caplog):
    """Fallback attribution logs the provider that produced the answer."""
    chain = ProviderChain([
        ReceiptProvider("primary", [""]),
        ReceiptProvider("secondary", ["answered"]),
    ])

    caplog.set_level(logging.INFO, logger="providers")
    assert chain.ask("test", timeout=1) == "answered"
    assert "Provider fallback: answered by secondary" in caplog.text


def test_chain_from_env_order_respects_env():
    claude = ClaudeProvider(bin="/bin/echo")
    codex = CodexProvider(bin="/bin/echo")
    os.environ["_TEST_ORDER"] = "codex,claude"
    try:
        chain = ProviderChain.from_env_order(
            "_TEST_ORDER", default="claude,codex",
            providers={"claude": claude, "codex": codex})
        assert chain.names() == ["codex", "claude"]
    finally:
        del os.environ["_TEST_ORDER"]


def test_chain_from_env_order_falls_back_to_default():
    claude = ClaudeProvider(bin="/bin/echo")
    codex = CodexProvider(bin="/bin/echo")
    # Env var is unset
    os.environ.pop("_NONEXISTENT_TEST_ENV", None)
    chain = ProviderChain.from_env_order(
        "_NONEXISTENT_TEST_ENV", default="codex,claude",
        providers={"claude": claude, "codex": codex})
    assert chain.names() == ["codex", "claude"]


def test_chain_from_env_order_skips_unknown():
    claude = ClaudeProvider(bin="/bin/echo")
    os.environ["_TEST_UNKNOWN_ORDER"] = "ghost,claude,phantom"
    try:
        chain = ProviderChain.from_env_order(
            "_TEST_UNKNOWN_ORDER", default="claude",
            providers={"claude": claude})
        # Unknown names are logged + dropped
        assert chain.names() == ["claude"]
    finally:
        del os.environ["_TEST_UNKNOWN_ORDER"]


def test_chain_ask_skips_unavailable_providers():
    """Providers reporting unavailable (e.g. unconfigured OpenCode) are silently skipped."""
    opencode = OpenCodeProvider(bin="/bin/echo", model="")  # unavailable
    claude = ClaudeProvider(bin="/bin/echo", retries=0)
    chain = ProviderChain([opencode, claude])

    mock_result = type("R", (), {"returncode": 0, "stdout": "claude won", "stderr": ""})()
    with patch("subprocess.run", return_value=mock_result):
        result = chain.ask("test", timeout=1)
    assert result == "claude won"


def test_chain_reset_failures_resets_all():
    p1 = ClaudeProvider(bin="/bin/echo")
    p2 = CodexProvider(bin="/bin/echo")
    p1.breaker.record_failure()
    p2.breaker.record_failure()
    chain = ProviderChain([p1, p2])
    chain.reset_failures()
    # Both providers' transient failure counts are cleared
    assert p1.is_available()
    assert p2.is_available()


def test_chain_strips_surrogates_before_dispatch():
    """The chain strips unpaired surrogates so downstream providers don't see them."""
    p = ClaudeProvider(bin="/bin/echo", retries=0)
    chain = ProviderChain([p])

    captured = []

    def fake_run(*args, **kwargs):
        captured.append(kwargs.get("input", ""))
        return type("R", (), {"returncode": 0, "stdout": "ok", "stderr": ""})()

    with patch("subprocess.run", side_effect=fake_run):
        chain.ask("hello \udcc4 world", timeout=1)
    assert captured
    assert "\udcc4" not in captured[0]


# ─── Tier resolution ───────────────────────────────────────────────────────


def _captured_subprocess_args():
    """Return a (calls, fake_run) pair. fake_run records each subprocess.run
    invocation's positional args (command list)."""
    calls = []

    def fake_run(*args, **kwargs):
        calls.append(args[0] if args else kwargs.get("args"))
        return type("R", (), {"returncode": 0, "stdout": "ok", "stderr": ""})()

    return calls, fake_run


def test_claude_tier_classification_uses_sonnet_low():
    """tier='classification' on Claude resolves to sonnet/low by default."""
    p = ClaudeProvider(bin="/bin/echo", default_effort="max", retries=0)
    calls, fake = _captured_subprocess_args()
    with patch("subprocess.run", side_effect=fake):
        p.ask("test", timeout=1, tier="classification")
    assert calls
    cmd = calls[0]
    # --effort low expected
    assert "--effort" in cmd and "low" in cmd
    # --model sonnet expected (tier preset injects it)
    assert "--model" in cmd and "sonnet" in cmd


def test_claude_per_call_overrides_tier():
    """Explicit per-call model/effort kwargs beat the tier preset."""
    p = ClaudeProvider(bin="/bin/echo", retries=0)
    calls, fake = _captured_subprocess_args()
    with patch("subprocess.run", side_effect=fake):
        p.ask("test", timeout=1, tier="classification", model="opus", effort="high")
    cmd = calls[0]
    assert "opus" in cmd
    assert "high" in cmd


def test_claude_no_tier_falls_through_to_defaults():
    """No-tier Claude calls name the exact creative model passed to the CLI."""
    p = ClaudeProvider(bin="/bin/echo", default_effort="max", retries=0)
    calls, fake = _captured_subprocess_args()
    with patch("subprocess.run", side_effect=fake):
        p.ask("test", timeout=1)
    cmd = calls[0]
    assert "max" in cmd
    assert cmd[cmd.index("--model") + 1] == "opus"
    assert p.resolved_call().model == "opus"


def test_codex_tier_classification_uses_luna_low_effort():
    """tier='classification' on Codex uses the shared cheap model and low effort.
    2026-07-10: gpt-5.3-codex-spark -> gpt-5.6-luna (5.6 family adoption)."""
    p = CodexProvider(bin="/bin/echo", model="gpt-5.5", effort="xhigh")
    calls, fake = _captured_subprocess_args()
    with patch("subprocess.run", side_effect=fake):
        p.ask("test", timeout=1, tier="classification")
    cmd = calls[0]
    # -c model_reasoning_effort=low expected
    assert any("model_reasoning_effort=low" in c for c in cmd)
    # classification tier intentionally overrides the construction model.
    assert "gpt-5.6-luna" in cmd
    assert "gpt-5.5" not in cmd


def test_codex_disables_plugins_and_project_docs():
    """Every codex exec call must carry --disable plugins and
    -c project_doc_max_bytes=0. Found 2026-07-09: the curated plugin
    marketplace (server-refreshed content — a supply-chain surface) injected a
    superpowers session-start ceremony into EVERY call (5 skill-file reads on a
    one-word reply, A/B verified), and codex auto-read a stale April CLAUDE.md
    from the workdir ('Claude CLI is primary') into every prompt."""
    p = CodexProvider(bin="/bin/echo")
    calls, fake = _captured_subprocess_args()
    with patch("subprocess.run", side_effect=fake):
        p.ask("test", timeout=1)
    cmd = calls[0]
    assert "--disable" in cmd
    assert "plugins" in cmd
    assert any("project_doc_max_bytes=0" in c for c in cmd)


def test_codex_construction_defaults_are_56_sol_xhigh():
    """No model/effort args -> the 5.6 flagship at xhigh (2026-07-11 downshift
    from ultra to stretch the shared sol+luna quota pool). luna calls go
    through the classification tier preset above, never these defaults."""
    p = CodexProvider(bin="/bin/echo")
    calls, fake = _captured_subprocess_args()
    with patch("subprocess.run", side_effect=fake):
        p.ask("test", timeout=1)
    cmd = calls[0]
    assert "gpt-5.6-sol" in cmd
    assert any("model_reasoning_effort=xhigh" in c for c in cmd)


def test_codex_per_call_model_overrides_construction():
    """Per-call model kwarg overrides the construction model."""
    p = CodexProvider(bin="/bin/echo", model="gpt-5.5", effort="xhigh")
    calls, fake = _captured_subprocess_args()
    with patch("subprocess.run", side_effect=fake):
        p.ask("test", timeout=1, model="gpt-5.6")
    cmd = calls[0]
    assert "gpt-5.6" in cmd
    # effort still xhigh (default)
    assert any("model_reasoning_effort=xhigh" in c for c in cmd)


def test_codex_no_tier_uses_construction_defaults():
    """With no tier and no overrides, Codex uses construction-time defaults."""
    p = CodexProvider(bin="/bin/echo", model="gpt-5.5", effort="xhigh")
    calls, fake = _captured_subprocess_args()
    with patch("subprocess.run", side_effect=fake):
        p.ask("test", timeout=1)
    cmd = calls[0]
    assert "gpt-5.5" in cmd
    assert any("model_reasoning_effort=xhigh" in c for c in cmd)


# ─── Codex web_search grounding gate ─────────────────────────────────────────
# Creative calls (a real tool allowlist, or None = "no restriction") must enable
# Codex's native web_search tool so takes are grounded. Tool-free calls pass the
# "" classification sentinel or the hard "__none__" text-only sentinel.


def test_codex_enables_web_search_with_tool_allowlist():
    """A non-empty tools allowlist (creative call) turns on Codex web_search."""
    p = CodexProvider(bin="/bin/echo", model="gpt-5.5", effort="xhigh")
    calls, fake = _captured_subprocess_args()
    with patch("subprocess.run", side_effect=fake):
        p.ask("test", timeout=1, tools="WebSearch,WebFetch,Read")
    cmd = calls[0]
    assert any("tools.web_search=true" in c for c in cmd)


def test_codex_enables_web_search_when_tools_none():
    """tools=None means the caller didn't restrict tools (creative default) → search ON."""
    p = CodexProvider(bin="/bin/echo", model="gpt-5.5", effort="xhigh")
    calls, fake = _captured_subprocess_args()
    with patch("subprocess.run", side_effect=fake):
        p.ask("test", timeout=1)  # tools defaults to None
    cmd = calls[0]
    assert any("tools.web_search=true" in c for c in cmd)


def test_codex_no_web_search_for_empty_tools_sentinel():
    """tools="" (classification "no tools") must NOT enable web_search."""
    p = CodexProvider(bin="/bin/echo", model="gpt-5.5", effort="xhigh")
    calls, fake = _captured_subprocess_args()
    with patch("subprocess.run", side_effect=fake):
        p.ask("test", timeout=1, tools="")
    cmd = calls[0]
    assert not any("tools.web_search=true" in c for c in cmd)


def test_codex_none_tools_sentinel_builds_hard_text_only_command():
    """The explicit sentinel must remove every Codex 0.144 tool surface."""
    captured = {}

    def fake_run(*args, **kwargs):
        captured["cmd"] = args[0]
        captured["cwd"] = kwargs["cwd"]
        temp_cwd = Path(kwargs["cwd"])
        assert temp_cwd.is_dir()
        assert list(temp_cwd.iterdir()) == []
        return type("R", (), {
            "returncode": 0,
            "stdout": "text-only response",
            "stderr": "",
        })()

    p = CodexProvider(
        bin="/bin/echo",
        model="gpt-5.5",
        effort="xhigh",
        cwd="/srv/ln-agent",
        permission_profile="benthic_bot_operator",
        add_dirs=["~/.claude"],
    )
    with patch("subprocess.run", side_effect=fake_run):
        assert p.ask("test", timeout=1, tools="__none__") == "text-only response"

    cmd = captured["cmd"]
    assert "--ignore-user-config" in cmd
    assert "--ignore-rules" in cmd
    assert "--sandbox" in cmd
    assert cmd[cmd.index("--sandbox") + 1] == "read-only"
    assert "approval_policy=never" in cmd
    assert 'web_search="disabled"' in cmd
    assert "tools.view_image=false" in cmd
    assert "--dangerously-bypass-approvals-and-sandbox" not in cmd
    assert "--add-dir" not in cmd
    assert not any(value.startswith("default_permissions=") for value in cmd)

    disabled = {
        cmd[index + 1]
        for index, value in enumerate(cmd[:-1])
        if value == "--disable"
    }
    assert disabled >= {
        "apps",
        "browser_use",
        "browser_use_external",
        "browser_use_full_cdp_access",
        "computer_use",
        "hooks",
        "image_generation",
        "in_app_browser",
        "memories",
        "multi_agent",
        "plugins",
        "remote_plugin",
        "shell_tool",
        "tool_suggest",
    }

    command_cwd = Path(cmd[cmd.index("-C") + 1])
    assert command_cwd == Path(captured["cwd"])
    assert command_cwd != Path("/srv/ln-agent")
    assert not command_cwd.exists()


@pytest.mark.parametrize("tools", [None, "WebSearch,WebFetch,Read", ""])
def test_codex_non_text_only_tool_modes_keep_existing_command_path(tools):
    """Creative calls, allowlists, and the classification sentinel stay compatible."""
    p = CodexProvider(
        bin="/bin/echo",
        cwd="/srv/ln-agent",
        permission_profile="benthic_bot",
    )
    calls, fake = _captured_subprocess_args()
    with patch("subprocess.run", side_effect=fake):
        p.ask("test", timeout=1, tools=tools)

    cmd = calls[0]
    assert "--ignore-user-config" not in cmd
    assert "--ignore-rules" not in cmd
    assert "default_permissions=benthic_bot" in cmd
    assert "approval_policy=never" in cmd
    assert cmd[cmd.index("-C") + 1] == "/srv/ln-agent"
    assert 'web_search="disabled"' not in cmd
    assert "tools.view_image=false" not in cmd


def test_chain_forwards_tier_kwarg():
    """ProviderChain.ask passes `tier=` through to the underlying provider."""
    codex = CodexProvider(bin="/bin/echo", model="gpt-5.5", effort="xhigh")
    chain = ProviderChain([codex])
    calls, fake = _captured_subprocess_args()
    with patch("subprocess.run", side_effect=fake):
        chain.ask("test", timeout=1, tier="classification")
    cmd = calls[0]
    assert any("model_reasoning_effort=low" in c for c in cmd)


def test_unknown_tier_silently_falls_through_to_defaults():
    """An unknown tier name doesn't error — it just doesn't apply any preset."""
    p = ClaudeProvider(bin="/bin/echo", default_effort="max", retries=0)
    calls, fake = _captured_subprocess_args()
    with patch("subprocess.run", side_effect=fake):
        p.ask("test", timeout=1, tier="this_tier_does_not_exist")
    cmd = calls[0]
    assert "max" in cmd


def test_tier_mutation_is_isolated_between_instances():
    """Mutating one provider's tier dict must not bleed into other instances
    or into the module-level default. Catches the shallow-copy aliasing trap."""
    from providers import _CLAUDE_DEFAULT_TIERS
    original_default = _CLAUDE_DEFAULT_TIERS["classification"]["effort"]

    p1 = ClaudeProvider(bin="/bin/echo")
    p1.tiers["classification"]["effort"] = "high"

    p2 = ClaudeProvider(bin="/bin/echo")
    assert p2.tiers["classification"]["effort"] == original_default
    assert _CLAUDE_DEFAULT_TIERS["classification"]["effort"] == original_default


def test_chain_forwards_tier_and_overrides_together():
    """ProviderChain.ask passes tier AND per-call overrides through, with the
    per-call kwargs winning over the tier preset."""
    p = ClaudeProvider(bin="/bin/echo", retries=0)
    chain = ProviderChain([p])
    calls, fake = _captured_subprocess_args()
    with patch("subprocess.run", side_effect=fake):
        chain.ask("test", timeout=1, tier="classification",
                  model="opus", effort="high")
    cmd = calls[0]
    # Per-call kwargs beat the classification preset (sonnet/low)
    assert "opus" in cmd
    assert "high" in cmd
    assert "sonnet" not in cmd
