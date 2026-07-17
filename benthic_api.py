#!/usr/bin/env python3
"""Benthic News API — standalone FastAPI service for SerenDB marketplace.

Exposes Benthic's curated crypto news feed and article analysis as paid
API endpoints. Read-only access to agent.db. Designed to run as its own
PM2 process alongside ln-agent and benthic-bot.
"""

import json
import logging
import logging.handlers
import os
import re
import shutil
import sqlite3
import subprocess
import time
import unicodedata
from pathlib import Path
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, Query, HTTPException, Request
from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints

from prompt_loader import load_prompt

# ─── Configuration ───────────────────────────────────────────────────────────

# BASE_DIR is the directory containing this file, used to resolve relative paths
# such as the SQLite database and log file.
BASE_DIR = Path(__file__).parent

# API_PORT: TCP port the server listens on. Default 8099, overridable via env.
API_PORT = int(os.environ.get("API_PORT", "8099"))

# API_DB_PATH: Path to the shared agent.db SQLite database (read-only access).
# The database is created/owned by ln-agent; this service never writes to it.
API_DB_PATH = Path(os.environ.get("API_DB_PATH", str(BASE_DIR / "agent.db")))

# API_RATE_LIMIT: Max requests per minute allowed for the expensive /analyze
# endpoint. Simple in-process sliding-window limiter, not cluster-safe.
API_RATE_LIMIT = int(os.environ.get("API_RATE_LIMIT", "10"))  # analyze reqs/min

# API_KEY: Static bearer token required for all endpoints. SerenDB sends this
# when proxying requests so we know they came through the billing gateway.
# Without this, anyone could bypass SerenDB and call the API for free.
API_KEY = os.environ.get("API_KEY", "").strip()
# Explicit opt-out for local development ONLY: runs the API with no auth.
# Without this flag an unset API_KEY refuses requests instead of serving an
# open paid endpoint by accident.
API_ALLOW_UNAUTHENTICATED = os.environ.get("API_ALLOW_UNAUTHENTICATED", "0") == "1"

# ─── Logging ─────────────────────────────────────────────────────────────────

# Rotating file handler: 10 MB max per file, 5 rotated backups — matches the
# log rotation policy used by ln-agent and benthic-bot for consistency.
LOG_FILE = BASE_DIR / "benthic-api.log"
log = logging.getLogger("benthic-api")
log.setLevel(logging.INFO)

_file_handler = logging.handlers.RotatingFileHandler(
    LOG_FILE, maxBytes=10_000_000, backupCount=5)
_file_handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
log.addHandler(_file_handler)

# Console handler for PM2 stdout/stderr capture and local dev visibility.
_console_handler = logging.StreamHandler()
_console_handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
log.addHandler(_console_handler)

# ─── Prompt Injection Defense ────────────────────────────────────────────────
# Mirrors benthic-bot.py / ln-agent.py defense stack. All checks use NFKD
# normalization to defeat homoglyph bypass (e.g. fullwidth chars).

INJECTION_OUTPUT_PATTERNS = [
    "ignore previous", "ignore all", "ignore above", "ignore the above",
    "disregard previous", "disregard all", "disregard above",
    "new instructions", "system prompt", "my instructions",
    "as an ai", "as a language model", "i'm an ai",
    # Secret-leak patterns — Claude CLI inherits the full environment, and a
    # crafted URL could trick it into leaking context. Matches benthic-bot.py.
    "ln-wallet", "telegram-creds", "agent_session", "ln-bot-token",
    "my wallet key is", "my private key is", "my api key is",
]

LEAK_PATTERNS = [
    "enough context", "i have enough context", "i have enough",
    "webfetch", "websearch", "twitter-explorer",
    "here's the comment", "here is the comment",
    "here's the reply", "here is the reply",
    "here's my", "here's my reply", "here is my reply",
    "let me search twitter", "let me search the web", "let me use webfetch",
    "let me check", "i'll search", "i'll use", "i need to",
    "i can't access", "i cannot access",
    "cookies appear", "cookies expired", "cookies are expired",
]

STRUCTURAL_LEAK_PATTERNS = ["tool_use", "tool_result", "function_call"]


def check_output_for_injection(text: str, context: str = "") -> bool:
    """Check if LLM output shows signs of prompt injection. Returns True if
    compromised. Uses NFKD normalization to defeat homoglyph bypass."""
    if not text:
        return False
    text_lower = unicodedata.normalize("NFKD", text).lower()
    for pattern in INJECTION_OUTPUT_PATTERNS:
        if pattern in text_lower:
            log.warning(f"INJECTION DETECTED in {context}: matched '{pattern}' — output: {text[:200]}")
            return True
    return False


def check_leak_patterns(text: str) -> bool:
    """Check if output contains Claude internal monologue. Returns True if leaked."""
    if not text:
        return False
    text_lower = unicodedata.normalize("NFKD", text).lower()
    if any(p in text_lower for p in LEAK_PATTERNS):
        log.warning(f"Rejected leaked output: {text[:80]}")
        return True
    return False


def check_structural_leaks(text: str) -> bool:
    """Check if output contains raw tool-call XML/JSON blocks. Always invalid
    in API responses — tools are disabled but this is belt-and-suspenders."""
    if not text:
        return False
    text_lower = unicodedata.normalize("NFKD", text).lower()
    if any(p in text_lower for p in STRUCTURAL_LEAK_PATTERNS):
        log.warning(f"Rejected structural leak: {text[:80]}")
        return True
    return False


# ─── URL Validation ──────────────────────────────────────────────────────────

def validate_url(url: str) -> str | None:
    """Validate and sanitize a URL. Rejects control chars, spaces, oversized
    URLs, non-HTTP schemes, and <> brackets. Copied from benthic-bot.py for
    standalone operation — no cross-imports.

    Returns the cleaned URL string on success, or None if validation fails.
    Stripping leading/trailing whitespace and quote characters prevents
    LLM-generated or user-supplied URLs from slipping through with padding.
    The <> stripping prevents XML boundary injection when URLs are later
    interpolated into prompts.
    """
    if not url:
        return None
    url = url.strip().strip('"\'')
    # Strip angle brackets to prevent XML boundary injection when this URL
    # is later interpolated into LLM prompts (e.g. craft_headline).
    url = url.replace("<", "").replace(">", "")
    # Oversized URLs are a sign of garbage input or a prompt-injection attempt
    # that tries to smuggle content inside a URL-shaped string.
    if len(url) > 2048:
        log.warning(f"Rejected oversized URL ({len(url)} chars): {url[:100]}...")
        return None
    # Control characters (\n \r \t \x00) can break HTTP headers and log lines,
    # and are never valid in a URL that will be used in an HTTP request.
    if any(c in url for c in '\n\r\t\x00'):
        log.warning(f"Rejected URL with control characters: {url[:100]}")
        return None
    # Spaces are invalid in URLs without percent-encoding; reject to be safe.
    if ' ' in url:
        log.warning(f"Rejected URL with spaces: {url[:100]}")
        return None
    try:
        parsed = urlparse(url)
        # Only allow http/https — ftp, javascript, data, etc. are all blocked.
        if parsed.scheme not in ("http", "https"):
            return None
        # A URL without a netloc is not a usable web address.
        if not parsed.netloc:
            return None
        return url
    except Exception:
        return None


# ─── Database ────────────────────────────────────────────────────────────────

def _get_db() -> sqlite3.Connection:
    """Open a read-only connection to agent.db.

    WAL (Write-Ahead Logging) mode allows this API process to read concurrently
    with ln-agent and benthic-bot writing to the same file without blocking.
    PRAGMA query_only=ON enforces that this connection never issues any DML or
    DDL — a belt-and-suspenders guard on top of the fact that nothing in this
    service calls conn.commit().

    Row factory is set to sqlite3.Row so callers can access columns by name
    (row["headline"]) as well as by index, which makes endpoint code cleaner.
    check_same_thread=False is safe here because each request gets its own
    connection object; we do not share a single connection across threads.
    """
    conn = sqlite3.connect(str(API_DB_PATH), check_same_thread=False)
    # WAL mode may fail if the DB file is read-only at the filesystem level
    # (ln-agent will have already set WAL). Log a warning but don't crash.
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        log.warning("Could not set WAL mode (DB may be read-only filesystem)")
    conn.execute("PRAGMA query_only=ON")
    conn.row_factory = sqlite3.Row
    return conn


# ─── Rate Limiting ───────────────────────────────────────────────────────────

class RateLimiter:
    """Simple sliding-window rate limiter. Safe for single-threaded single-worker
    uvicorn only. Not safe with --workers N or thread pool executors."""

    def __init__(self, max_requests: int, window_seconds: float = 60):
        self.max_requests = max_requests
        self.window = window_seconds
        self._timestamps: list[float] = []

    def allow(self) -> bool:
        """Return True if the request is within rate limits."""
        now = time.time()
        # Evict timestamps outside the sliding window
        self._timestamps = [t for t in self._timestamps if now - t < self.window]
        if len(self._timestamps) >= self.max_requests:
            return False
        self._timestamps.append(now)
        return True


# Global limiter instance for /analyze. Uses API_RATE_LIMIT from config (default 10 req/min).
# Replaced in tests via benthic_api._analyze_limiter = RateLimiter(...) to control limits.
_analyze_limiter = RateLimiter(max_requests=API_RATE_LIMIT, window_seconds=60)


# ─── LLM Provider Layer ─────────────────────────────────────────────────────
# Provider chain driven by PROVIDER_ORDER env var (default codex,claude).
# Each provider has its own circuit breaker; failures don't penalize others.

CLAUDE_BIN = os.environ.get("CLAUDE_BIN",
    shutil.which("claude") or str(Path("~/.local/bin/claude").expanduser()))


def _resolve_codex_bin() -> str:
    """Resolve Codex binary even when PM2/login shells do not preload the NVM path."""
    found = shutil.which("codex")
    if found:
        return found
    candidates = sorted(Path("~/.nvm/versions/node").expanduser().glob("*/bin/codex"))
    if candidates:
        return str(candidates[-1])
    return "codex"


CODEX_BIN = os.environ.get("CODEX_BIN", _resolve_codex_bin())
CODEX_MODEL = os.environ.get("CODEX_MODEL", "gpt-5.6-sol")
CODEX_EFFORT = os.environ.get("CODEX_EFFORT", "xhigh")
CLAUDE_LIMIT_COOLDOWN = int(os.environ.get("CLAUDE_LIMIT_COOLDOWN", str(6 * 60 * 60)))

from providers import ClaudeProvider, CodexProvider, ProviderChain


def _api_codex_wrapper(prompt: str) -> str:
    return load_prompt("api/codex_wrapper", prompt=prompt)


_claude_provider = ClaudeProvider(
    bin=CLAUDE_BIN,
    default_model="sonnet",
    default_effort="low",
    default_tools="__none__",
    cwd=str(BASE_DIR),
    quota_cooldown=CLAUDE_LIMIT_COOLDOWN,
)
_codex_provider = CodexProvider(
    bin=CODEX_BIN,
    model=CODEX_MODEL,
    # API is classification-only by design — force low effort regardless of
    # the global CODEX_EFFORT env. The API profile denies all ~/.claude creds.
    effort="low",
    cwd=str(BASE_DIR),
    sandbox_bypass=False,
    permission_profile="benthic_api",
    wrapper=_api_codex_wrapper,
)
_provider_chain = ProviderChain.from_env_order(
    "PROVIDER_ORDER", default="codex,claude",
    providers={"claude": _claude_provider, "codex": _codex_provider},
)
log.info(f"LLM provider chain: {','.join(_provider_chain.names())}")


def llm_ask(prompt: str, timeout: int = 120) -> str:
    """Dispatch to the provider chain. The API only runs classification-tier
    calls, so each provider's construction defaults already reflect that —
    no per-call tier override needed."""
    # Classification-only: pass the no-tools sentinel so CodexProvider does NOT turn
    # on web_search (tools=None would, under the codex,claude default) — keeps API
    # analysis deterministic and low-latency, and lets the caller timeout hold — PR #1 finding.
    return _provider_chain.ask(prompt, timeout=timeout, tools="__none__")


# ─── App ─────────────────────────────────────────────────────────────────────

# ─── Auth ────────────────────────────────────────────────────────────────────

def _verify_api_key(request: Request):
    """Verify the bearer token matches our static API key.
    SerenDB sends this header when proxying requests through the billing gateway.
    Without this check, anyone could bypass SerenDB and call the API for free.
    If API_KEY is unset, requests are refused (fail closed) unless the operator
    explicitly opted into unauthenticated mode via API_ALLOW_UNAUTHENTICATED=1."""
    if not API_KEY:
        if API_ALLOW_UNAUTHENTICATED:
            return  # Explicit local-dev opt-in — open access
        raise HTTPException(
            status_code=503,
            detail="API_KEY not configured. Set API_KEY, or "
                   "API_ALLOW_UNAUTHENTICATED=1 for local development only.",
        )
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        token = auth[7:].strip()
    else:
        token = auth.strip()
    if token != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


# FastAPI instance — title/version appear in the auto-generated /docs (Swagger)
# and /openapi.json, which SerenDB uses to display marketplace endpoint info.
app = FastAPI(title="Benthic News API", version="1.0.0")


@app.get("/health")
def health():
    """Standard healthcheck. No auth required — used by uptime monitors."""
    return {"status": "ok"}


@app.get("/news", dependencies=[Depends(_verify_api_key)])
def get_news(
    limit: int = Query(default=20, ge=1, le=100, description="Max results (cap 100)"),
    offset: int = Query(default=0, ge=0, description="Pagination offset"),
    since: str | None = Query(default=None, description="ISO 8601 timestamp — only articles after this time"),
):
    """Curated headline feed from Benthic's pipeline. Pure SQLite read.

    Returns articles sorted newest-first (posted_at DESC). Use `limit` and
    `offset` for pagination. Use `since` to poll for new articles since a
    known timestamp (ISO 8601, e.g. 2026-04-11T12:00:00Z).

    `limit` values above 100 are silently capped to 100 to prevent runaway
    reads on large databases. `has_more` is True when additional pages exist
    beyond the current slice.
    """
    # Defense-in-depth: FastAPI's le=100 rejects values >100 with 422,
    # but cap here too in case the Query constraint is ever loosened.
    limit = min(limit, 100)

    try:
        conn = _get_db()
    except Exception as e:
        log.error(f"Database unavailable: {e}")
        return {"articles": [], "total": 0, "has_more": False}

    try:
        # Build optional WHERE clause for the `since` filter.
        # posted_at is stored as ISO 8601 text so lexicographic comparison works
        # correctly as long as the timestamps are in UTC with the same format.
        # Base filter: exclude system markers ([duplicate in HQ], [stale], etc.)
        # and NULL/empty headlines — these are rejected articles that never made
        # it to the main LN feed. Only serve real editorial content to consumers.
        conditions = ["headline IS NOT NULL", "headline != ''", "headline NOT LIKE '[%'"]
        params: list = []
        if since:
            # Validate by actually parsing — rejects nonsense like "2026-04-11T:::ZZZ"
            try:
                from datetime import datetime as _dt, timezone as _tz
                _dt_since = _dt.fromisoformat(since.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                raise HTTPException(status_code=400, detail="Invalid since format. Use ISO 8601 (e.g. 2026-04-11T00:00:00Z)")
            # The DB stores datetime.now(timezone.utc).isoformat() strings, so
            # normalize the caller's offset to UTC before the lexicographic
            # compare; naive input is treated as UTC.
            if _dt_since.tzinfo is None:
                _dt_since = _dt_since.replace(tzinfo=_tz.utc)
            conditions.append("posted_at > ?")
            params.append(_dt_since.astimezone(_tz.utc).isoformat())
        where = "WHERE " + " AND ".join(conditions)

        # Gracefully handle missing table (agent.db exists but ln-agent hasn't run yet)
        try:
            conn.execute("SELECT 1 FROM posted_articles LIMIT 1")
        except sqlite3.OperationalError:
            log.warning("posted_articles table does not exist yet")
            return {"articles": [], "total": 0, "has_more": False}

        # Total count of matching rows — computed before applying LIMIT/OFFSET
        # so the caller can calculate total pages without a second request.
        total = conn.execute(
            f"SELECT COUNT(*) FROM posted_articles {where}", params
        ).fetchone()[0]

        # Fetch the requested page, newest article first.
        rows = conn.execute(
            f"""SELECT id, headline, url, source_channel, posted_at, ln_article_id
                FROM posted_articles {where}
                ORDER BY posted_at DESC
                LIMIT ? OFFSET ?""",
            params + [limit, offset],
        ).fetchall()

        # Prefer ln_article_id (the LN platform's canonical ID) when available.
        # Fall back to the local SQLite row id for articles that failed to post
        # or were posted before the column was populated.
        articles = [
            {
                "article_id": row["ln_article_id"] or row["id"],
                "headline": row["headline"],
                "url": row["url"],
                "source": row["source_channel"] or "",
                "posted_at": row["posted_at"],
            }
            for row in rows
        ]

        return {
            "articles": articles,
            "total": total,
            # has_more is True when the slice doesn't exhaust the result set.
            # Using offset + len(articles) rather than offset + limit handles
            # the last partial page correctly (where len < limit).
            "has_more": (offset + len(articles)) < total,
        }
    finally:
        conn.close()


class AnalyzeRequest(BaseModel):
    url: str


class AnalyzeResponse(BaseModel):
    """Response schema for /analyze — whitelists fields to prevent LLM leakage.
    Field bounds prevent LLM hallucination from producing unbounded responses."""
    newsworthy: bool
    score: int = Field(ge=1, le=10)
    summary: str = Field(max_length=500)
    tags: list[Annotated[str, StringConstraints(max_length=50)]] = Field(max_length=20)
    primary_source: str = Field(max_length=2048)


@app.post("/analyze", response_model=AnalyzeResponse, dependencies=[Depends(_verify_api_key)])
def analyze_url(req: AnalyzeRequest):
    """Evaluate a URL for crypto/DeFi newsworthiness via Claude CLI.

    Validates the URL, applies a sliding-window rate limit, then shells out to
    Claude CLI (Sonnet/low/no-tools) and returns structured JSON. Returns 400 on
    bad URLs, 429 when rate limited, and 503 when the Claude CLI is unavailable
    or returns unparseable output.
    """
    # Validate URL — rejects control chars, spaces, oversized, non-HTTP schemes
    clean_url = validate_url(req.url)
    if not clean_url:
        raise HTTPException(status_code=400, detail="Invalid URL")

    # Sliding-window rate limit — rejects when _analyze_limiter is exhausted
    if not _analyze_limiter.allow():
        raise HTTPException(status_code=429, detail="Rate limit exceeded. Try again later.")

    # Build analysis prompt from external template — URL wrapped in <user_content>
    # tags with explicit security warning, matching benthic-bot.py / ln-agent.py
    # defense pattern. The URL is user-supplied and UNTRUSTED — it may contain
    # adversarial text in the path designed to manipulate the LLM's output.
    prompt = load_prompt("api/analyze", clean_url=clean_url)

    # Call LLM provider layer — Claude primary, Codex fallback, with retry,
    # circuit breaker, and quota detection. Matches benthic-bot.py's llm_ask().
    raw = llm_ask(prompt)
    if not raw:
        raise HTTPException(status_code=503, detail="Analysis service temporarily unavailable")

    # ── Output defense: check for injection, leaks, structural leaks ──
    # Matches benthic-bot.py defense stack. NFKD-normalized pattern matching
    # prevents homoglyph bypass. All checks run before the response reaches
    # the API consumer.
    if check_output_for_injection(raw, context="analyze"):
        raise HTTPException(status_code=503, detail="Analysis service temporarily unavailable")
    if check_leak_patterns(raw):
        raise HTTPException(status_code=503, detail="Analysis service temporarily unavailable")
    if check_structural_leaks(raw):
        raise HTTPException(status_code=503, detail="Analysis service temporarily unavailable")

    # 3-pass JSON extraction matching ln-agent.py's _extract_json_array pattern:
    # Pass 1: try raw parse. Pass 2: extract from markdown fences.
    # Pass 3: find first '{' and last '}' — model may have written prose around JSON.
    result = None
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        pass

    if result is None:
        fence_match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', raw, re.DOTALL)
        if fence_match:
            try:
                result = json.loads(fence_match.group(1))
            except json.JSONDecodeError:
                pass

    if result is None:
        # Pass 3: bracket search — find outermost { } in the response
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end > start:
            try:
                result = json.loads(raw[start:end + 1])
            except json.JSONDecodeError:
                pass

    if result is None:
        log.warning(f"Failed to parse LLM JSON (all 3 passes): {raw[:200]}")
        raise HTTPException(status_code=503, detail="Analysis service temporarily unavailable")

    # Validate and whitelist response fields via Pydantic — catches missing fields,
    # wrong types (e.g. score="seven"), and strips any extra LLM output fields.
    # Also prevents any extra keys Claude might return from leaking to consumers.
    try:
        return AnalyzeResponse(
            newsworthy=result["newsworthy"],
            score=result["score"],
            summary=result["summary"],
            tags=result["tags"],
            primary_source=result["primary_source"],
        )
    except (KeyError, ValueError, TypeError) as e:
        log.warning(f"Claude response failed validation: {e} — raw: {result}")
        raise HTTPException(status_code=503, detail="Analysis service temporarily unavailable")


# ─── Entrypoint ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    if not API_KEY and not API_ALLOW_UNAUTHENTICATED:
        raise SystemExit(
            "ERROR: API_KEY is required — set it to the static bearer token your "
            "gateway sends, or set API_ALLOW_UNAUTHENTICATED=1 to run an open "
            "instance for local development only.")
    log.info(f"Starting Benthic API on port {API_PORT}")
    # Single worker — circuit breaker and rate limiter are module-level globals,
    # not safe with multiple workers. host="0.0.0.0" for container reachability.
    uvicorn.run(app, host="0.0.0.0", port=API_PORT, workers=1)
