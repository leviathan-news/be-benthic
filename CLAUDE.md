# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is the **Leviathan News (LN)** workspace — a white-label crypto/DeFi news agent system. Fork it, set `AGENT_NAME`, and run your own instance.

1. **`ln-agent.py`** — Automated news agent (PM2 name `ln-agent`) with 1-hour cycle sleep. Monitors Telegram channels (configured via `CHANNELS` env var), evaluates newsworthiness via the provider chain (`providers.py` — Codex primary, Claude fallback by default), deduplicates (URL dedup + self-dedup + provenance short-circuit + Bot HQ dedup), crafts headlines, posts via LN API. Also votes, writes TL;DR comments, runs the flag-gated market-match phase, and hosts the live-news WS listener.

2. **`benthic-bot.py`** — Telegram chat bot (PM2 name `benthic-bot`) sharing brain/identity with ln-agent. Responds in groups and operator DMs. Features: evidence-grounded replies, per-chat memory isolation, tiered operator auth, [GROUP] directive routing, agent-chat API polling, message debouncing, media analysis, persistent memory, self-awareness tracking, runtime-mediated sandbox, breaking-news reactions.

3. **`benthic-builder.py`** *(optional daemon)* — Codex-powered build queue. Consumes `build_tasks` rows from the shared SQLite, drives a goal-driven loop over `codex app-server` (exec fallback) in a per-task workdir, gates on review + independent VERIFY, and pushes the result as a public repo under `BUILD_GITHUB_ORG` via `github_client.sh repo push`. Each task is isolated, time-capped at 6h, and reports back to the originating Telegram chat. Operator CLI: `bin/benthic-build`.

4. **`reply_grounding.py`** — Evidence-grounding contracts for bot replies: strict reply/verifier/media parsers, typed evidence bundles, exact X-status extraction, and the proxy-free pinned HTTP fetcher. Imported by `benthic-bot.py`.

5. **`providers.py`** — Provider-agnostic LLM dispatch layer. Defines `ClaudeProvider` / `CodexProvider` / `OpenCodeProvider` (each with its own `CircuitBreaker` + tier table) and `ProviderChain` (ordered fallback, env-driven via `PROVIDER_ORDER`). All runtime components import from here; swapping the "primary brain" is a single env var change, no code edits.

6. **`prompt_loader.py`** — Shared prompt template loader. Reads `.md` files from `prompts/`, caches in-memory, fills `{placeholders}` via `str.format()`. Used by all runtime components.

7. **`prompts/`** — External prompt template directory. All LLM prompts extracted from the agent files for dev-time context savings and clean separation. Subdirs: `agent/`, `bot/` (+ `knowledge/` topic files), and `_shared/`.
   Creative-tier prompts inject the shared anti-slop block from `prompts/_shared/no_ai_slop.md` via `{no_slop}`.

8. **`github_client.sh`** — Write-only GitHub client wrapper. Enforces repo allowlist (`~/.claude/.github-repos-allowlist`), rate limits non-operators (default 30/day, override via `RATE_LIMIT_MAX`), adds attribution footer. Supports: `issue create|close|comment|edit`, `pr create|comment`, `repo create|push`, `allowlist add|list`. Token loaded from `~/.claude/.github-token`. New repos auto-added to allowlist under `GITHUB_ORG` (env).

9. **`codex-policy/`** — Codex permission profiles + execpolicy rules for locked-down Codex invocations: every `~/.claude` secret is denied in-sandbox while allowlisted wrapper scripts run outside it. Ready to append to the deploy host's `~/.codex/config.toml` and `~/.codex/rules/`.

10. **`skills/leviathan-headlines/`** — Claude Code plugin skill for manual headline crafting and review following LN editorial standards.

## Architecture: ln-agent.py

Single-file Python agent running in a continuous loop (`run_loop`) with a flat 1-hour sleep after each completed cycle. Each cycle has six phases (the sixth is flag-gated), with Phase 3 running articles in parallel threads:

1. **Read** — Connect to Telegram via Telethon, fetch new messages from `CHANNELS` list using cursor-based pagination (stored in SQLite). Channel usernames are resolved to numeric IDs and cached to avoid flood waits.
2. **Evaluate** — Batch all messages to the provider layer (`providers.py`) for newsworthiness scoring and story-level deduplication. `PROVIDER_ORDER` defaults to `codex,claude`: Codex (`gpt-5.6-sol`/xhigh for creative work and `gpt-5.6-luna`/low for classification) is primary; Claude (Opus/max and Sonnet/low respectively) is the automatic fallback. Each provider has an independent circuit breaker, and the chain tries the next available provider after an empty or failed result. OpenCode is dormant unless configured and explicitly added to `PROVIDER_ORDER`. The active model is instructed to use WebSearch/WebFetch/twitter-explorer to verify stories and find primary sources. Output is strict JSON.
3. **Post** — For each newsworthy item: check DB for URL duplicate, self-dedup via `was_story_posted()` (word overlap on story_hint + headline against last 24h of own posts — catches same story from different sources), check Bot HQ for duplicates, verify freshness, resolve URL to primary source, craft headline + TL;DR in one creative-tier call, submit via LN API (`/api/v1/news/post`), auto-upvote, add TL;DR comment.
4. **Vote/Comment** — Fetch recent approved articles from LN API, evaluate quality via the provider layer, vote up/down, write analysis comments on uncommented articles. Also votes on other users' comments (yaps).
5. **Reply Detection** — Separate pass over last 20 articles we've commented on (regardless of age). Detects unreplied responses to our comments via `walk_replies_and_respond()`, crafts replies with prompt injection defense + sentinel verification. Skips articles already processed in Phase 4.
6. **Market match** (flag-gated by `ENABLE_MARKET_MATCH`, default off) — Polls squid-bot's `GET /api/v1/agent/queue/?needs_market=true`, and for each approved article decides **attach** (an existing open market; autonomous; reversible FK), **propose** (a new market, for operator approval — benthic NEVER mints), or **skip** (an audited reason), POSTing to `POST /api/v1/agent/market-match/<id>/`. A cheap classification-tier pre-filter gates the expensive matcher; per-cycle cap is `MARKET_MATCH_MAX_PER_CYCLE`. Phase 3 also pre-attaches an open market to benthic's own posts, via the `market_id` field on `/news/post`. Depends on squid-bot PR #380 deployed; benthic's LN user must be `is_staff=True`.

7. **Live-news WS listener** (flag `ENABLE_WS_EVENTS`, default on) — a supervised asyncio task in `run_loop` (OUTSIDE the cycle watchdog) holds `wss://api.leviathannews.xyz/ws/news/` (**Origin header required** — the handshake 403s without `Origin: https://leviathannews.xyz`), filters events to `WS_EVENT_TYPES` (default `news.approved`), and queues them in the `ws_events` table. Between full cycles, a throttled **mini-pass** (`ENABLE_WS_MINI_PASS`, min interval `MINI_PASS_MIN_INTERVAL`=600s, deadline `MINI_PASS_DEADLINE`=900s, cap `MINI_PASS_MAX_ARTICLES`=5) runs the extracted `vote_comment_pass()` on **queued ids only, consume-on-drain** — votes/comments land minutes after approval instead of up to an hour later. The gap flag (reconcile frames / reconnects / startup) is informational: gap backfill belongs to the hourly cycle, which polls the full feed, retires the queue, and clears the flag (a gap-widened mini-pass blew its deadline in prod 2026-07-02 — do not reintroduce). Liveness: protocol ping/pong ONLY (`WS_PING_INTERVAL`=20s, `WS_PING_TIMEOUT`=45s — generous so a transiently-stalled origin isn't declared dead). There is deliberately NO app-level recv timeout: the server's documented 25s heartbeats pause in practice, and the old 300s stall backstop was killing provably-healthy quiet connections — do not reintroduce one. Reconnect: exponential backoff + jitter capped at 300s; a connection that lived ≥`WS_STABLE_SECONDS` (60s) resets backoff to base (flaky-but-working streams must not ratchet to permanent 300s gaps); close code 4003 (capacity) starts at the cap. Requires `websockets` in the venv — if missing, the listener disables itself and the agent runs poll-only.

### Key Classes
- **`AgentDB`** — SQLite wrapper (WAL mode) with tables: `channel_cursors`, `channel_ids`, `evaluated_messages`, `posted_articles`, `commented_articles`, `voted_articles`, `voted_yaps`, `replied_yaps`, `runs`, `market_decisions`, `ws_events`
- **`LNClient`** — LN API client with wallet-based auth (nonce → sign → verify → JWT). Endpoints: `/news/post`, `/news/{id}/vote`, `/news/{id}/post_yap`, `/news/{id}/list_yaps`

### AI Evaluation Functions
All AI calls go through `llm_ask()`, a provider wrapper with two tiers:
- **Creative tier** — Codex `gpt-5.6-sol`/xhigh is primary. This tier uses the soul prompt and the task's permitted tools for headlines, TL;DR, analysis, and replies.
  **Claude creative fallback** uses Opus/max when needed.
- **Classification tier** — Shared Codex callers use `gpt-5.6-luna`/low. Benthic overrides its classification tier to `gpt-5.6-terra`/medium for pre-screening, sentinel checks, and grounded-reply verification. This tier uses no soul prompt and normally no tools for votes, freshness, duplicate checks, and reply worthiness.
  **Claude classification fallback** uses Sonnet/low when needed.

`llm_ask(prompt, timeout, tier, model, effort, skip_soul, tools)` dispatches through the configurable `PROVIDER_ORDER` (default: `codex,claude`). OpenCode participates only when it has a configured model and is explicitly added to the order. `"__none__"` is the hard text-only sentinel: Claude receives a non-matching tool allowlist, while Codex uses the isolated invocation documented below.

Key functions:
- `evaluate_and_deduplicate()` — batch evaluation, returns JSON array
- `batch_evaluate_articles()` / `batch_evaluate_comments()` — JSON batch eval for Phase 4 (reduces LLM calls)
- `_extract_json_array()` — 3-pass JSON extraction (fences, raw parse, bracket search)
- `_pre_filter_message()` — keyword pre-filter before LLM eval (skips obvious noise)
- `resolve_craft_headline_tldr()` — single creative-tier call: resolves primary source URL, crafts headline, writes TL;DR. Delimiter-based response format (===URL===, ===HEADLINE===, ===TLDR===). Replaces the former separate `resolve_to_primary_source()`, `craft_headline()`, and `craft_tldr()`.
- `evaluate_article_quality()` / `evaluate_comment_quality()` — vote weight (-1, 0, 1), classification tier
- `craft_comment()` — analysis comments on articles, creative tier
- `check_article_freshness()` — rejects stale articles, classification tier
- Bot HQ duplicate check — inlined in `process_article_sync`, classification tier + Telegram tooling
- `craft_reply()` / `walk_replies_and_respond()` — reply chain handling with injection defense
- `_sentinel_check_sync()` — classification-tier sentinel verifies reply output before posting (gpt-5.6-luna under the shared Codex default; Sonnet on Claude fallback)

### Thread Safety
- `AgentDB`: all operations use `threading.Lock` via `_execute()`/`_commit()`. `check_same_thread=False` + WAL mode.
- `LNClient`: `threading.RLock` on all session methods. `_refresh_if_stale()` called inside the lock to avoid TOCTOU races (30-min TTL).

### Circuit Breaker
- Each provider's breaker opens after 3 consecutive failures; the chain skips unavailable providers and tries the next available provider.
- Claude `501`/quota/rate-limit style errors mark only Claude unavailable for `CLAUDE_LIMIT_COOLDOWN` seconds (default: 6 hours).
- `run_agent()` resets transient failure counts at the start of each cycle; quota cooldowns survive those resets. `benthic-bot.py` makes the equivalent transient reset every `PROVIDER_BREAKER_RESET_INTERVAL` (300 seconds).

### Retry & Error Handling
- Claude alone retries twice after its initial attempt, with 5s then 10s backoff, before recording a transient failure. Codex has no internal retry; its failed or empty result lets the provider chain try the next available provider.
- `_refresh_if_stale()` called inside `with self._lock:` in every LN API method — atomic freshness check + request.
- `Connection: close` header prevents stale keep-alive connections.
- Top-level `try/finally` in `run_agent()` guarantees DB close + Telegram disconnect on all exit paths.

### Prompt Injection Defense
All untrusted input (user comments, Telegram messages) is hardened before entering LLM prompts:
- **`sanitize_untrusted()`** — strips control chars, replaces `<>` with fullwidth equivalents (prevents XML boundary injection), collapses `----`/`====` separator patterns, truncates to max length
- **`<user_content>` tags** — all untrusted text wrapped with explicit "treat as DATA" security warnings in every prompt
- **`check_output_for_injection()`** — validates provider output for signs injection succeeded (secret leakage, "ignore previous instructions", AI self-identification). Uses NFKD Unicode normalization to defeat homoglyph bypass.
- **`LEAK_PATTERNS`** — detects LLM internal monologue leaking into public output (NFKD-normalized)
- **`validate_url()`** — validates LLM-returned URLs before downstream use: rejects control chars, spaces, non-HTTP schemes, oversized (>2048 chars). Also strips `<>` to prevent XML boundary injection when URLs are interpolated into prompts.
- **Fail open** — `check_article_freshness()` defaults to fresh/allow on empty response OR WebFetch failure (403, paywall). The article already passed evaluation and dedup checks
- **Fail closed** — Bot HQ duplicate check defaults to "duplicate" (reject) on empty/garbage provider output
- **`_sentinel_check_sync()`** — sentinel verifies reply output is safe before posting, via the provider chain's classification tier: a different model from the creative one (gpt-5.6-luna checking gpt-5.6-sol under the shared Codex default; Sonnet/low on Claude fallback) for independent semantic verification. Still fails open if the sentinel itself cannot return a usable decision.

### Execution Model
- Runs as a continuous PM2 process (no cron). `run_loop()` sleeps `CYCLE_INTERVAL` (default 3600s) after each cycle.
- PM2 `autorestart: true` recovers from crashes. No `cron_restart` — avoids killing long runs.
- Phase 3 articles process in parallel via `asyncio.gather` + `asyncio.to_thread`.

## Architecture: benthic-bot.py

Telegram Bot API chat agent sharing brain/identity with ln-agent. Uses `getUpdates` long polling.

### Evidence-bounded reply pipeline

`reply_grounding.py` owns strict reply/verifier/media parsers, evidence
contracts, exact X-status extraction, and the proxy-free pinned HTTP fetcher.
`benthic-bot.py` owns engagement, provider calls, SQLite traces, directives,
finalization, and publication. Research and selected-image observation may use
bounded read tools, but public composition, verification, repair, and
sandbox-result synthesis use `tools="__none__"`.

Anchored natural vocatives of the agent's name — from `AGENT_NAME`, e.g.
`Benthic ...`, `Benthic, ...`, `Hey, Benthic bot ...` — count as direct without matching incidental mentions,
filenames, or URLs. The tools-disabled Terra engagement prompt receives the
exact sanitized direct-reply target body plus media marker even when the target
has fallen out of the five-message snippet. `BENTHIC_ENGAGEMENT_TIMEOUT`
defaults to 120 seconds and clamps to `30..300`; direct classifier failure
still engages while ambient classifier failure stays silent.

Source dispatch canonicalizes before cache lookup. Exact X status variants on
the X/Twitter roots or any dot-delimited subdomain (including standard ports,
trailing-dot hosts, and `/photo/N` or `/video/N`) use the Twitter extractor
only; generic HTTP rejects those hosts on initial and redirect hops. Explicit
background sources use a whole line containing exactly `Background: URL` or
`Background only: URL` (case-insensitive). Ambiguous background prose fails
closed. A failed declared background URL keeps its turn-local source slot and
its typed failure is cached, so the second evidence build does not request it
again. Turn-wide limits cover focal URL count, source request count, and fetched
response bytes; the remaining byte allowance is applied inside HTTP/X capture.
Actual bytes are debited during capture even when later decoding, HTML/JSON
parsing, or focal validation rejects the source. Per-transport counters prevent
late bytes from a failed capture from replacing the next source's debit. Cache
hits remain available after the deadline because they perform no new transport.
One absolute source deadline covers discovery, provider fallback, Claude retries,
and transport. The 960-second default gives a fresh Sol/xhigh discovery call up
to 900 seconds while retaining at least 30 seconds for fallback and 30 seconds
for trusted source transport. Typed `research_unavailable` and
`source_collection_timeout` reasons are persisted only when composition
abstains; a separately supported verified reply is not marked failed.
Generic HTML accepts only evaluable `display` and `visibility` declarations:
unknown inline CSS suppresses the element, unknown page CSS rejects the page,
closed `dialog` ancestors are hidden, and `head`, `title`, and meta-description
content never becomes visible body evidence. DNS resolver workers and Twitter
child/reaper cleanup are process-wide bounded; a killed child receives a
nonblocking reap attempt and otherwise transfers to bounded daemon cleanup.
The process slot is returned only after that child is confirmed reaped; failed
reaping quarantines capacity rather than allowing unbounded replacement children.

Selected image observation is path-exact: Codex receives only validated `-i`
attachments and has `view_image` disabled, while Claude receives exact
`Read(path)` rules. Evidence and metadata traces preserve separate sanitized
artifact and observation-text hashes, never paths or bytes. Pipeline results
name the final composer and final verifier explicitly. After repair, the second
verifier sees original evidence plus the repair provider receipt, and trace
composer fields identify the repair provider. Claude creative fallback always
passes explicit Opus/max; classification remains Sonnet/low.

Ambient messages publish only after a useful reply passes factual support.
Direct mentions/replies still engage, but missing focal/media evidence or a
twice-rejected reply returns deterministic uncertainty. No failure path may
substitute older chat, another same-author post, cached prose, or a previous
answer for missing focal evidence. Meaning-preserving paraphrases are allowed
without relaxing actor, source, quantity, or attribution checks. A
chronological older/newer claim is accepted only when the cited evidence
timestamp is ordered earlier/later than the relevant comparison-source
timestamp. If either timestamp is absent or ordering does not support the
claim, verification fails. Public prose need not print timestamps. An opinion
fixture must contain its referent.

M0 and R1 define the current requested task. Runtime receipts, older chat, and
background sources may support that task but cannot redefine it. Composition,
repair, and verification reject factually supported but materially
non-responsive replies, especially stale grievances, old self-critiques, or
provider failures substituted for the requested action. A materially useful
supported subset remains responsive when unsupported requested parts receive a
natural scoped limitation. A current concrete blocker is required for a
decline.

`chat_history.timestamp` is observed/ingestion time. Nullable `event_time`
stores normalized UTC Telegram/API event time for incoming chronology; legacy
or malformed values remain null, while stored bot replies may use observed send
time. An explicit null API event time never falls back to numeric `date`, and a
merged turn's latest message ID uses the latest fragment's matching event time.
Adjacent message fragments merge only when sender, chat, topic, and direct reply
target are all equal. Both Telegram and agent-chat ingress normalize an
ambiguous empty generation identically: concise neutral clarification for
direct turns and silence for ambient turns. Provider-error wording is emitted
only from a typed grounding result.

Metadata-only diagnostics:

    sqlite3 -readonly agent.db \
      'SELECT created_at,mode,disposition,failure_reason,composer_provider,verifier_provider FROM reply_grounding_traces ORDER BY created_at DESC LIMIT 20;'

Focused verification:

    .venv/bin/python3 -m pytest tests/test_reply_grounding.py tests/test_reply_grounding_pipeline.py tests/test_providers.py tests/test_photo_retrieval.py tests/test_prompt_templates.py tests/test_no_ai_slop.py tests/test_concurrent_processing.py -q
    .venv/bin/python3 scripts/eval_reply_grounding.py

Configuration defaults are `ENABLE_REPLY_GROUNDING=1`,
`GROUNDING_MAX_BACKGROUND_SOURCES=3`,
`GROUNDING_MAX_FOCAL_URLS=8`, `GROUNDING_MAX_SOURCE_REQUESTS=10`,
`GROUNDING_MAX_SOURCE_BYTES=2097152`,
`GROUNDING_SOURCE_COLLECTION_TIMEOUT=960`,
`GROUNDING_MAX_EVIDENCE_BYTES=24000`, `GROUNDING_FETCH_TIMEOUT=15`,
`GROUNDING_TRACE_RETENTION_DAYS=14`, `PHOTO_REFERENCE_MAX_AGE=1800`, and
`BENTHIC_ENGAGEMENT_TIMEOUT=120` (bounded to `30..300`).
Creative Codex defaults remain `CODEX_MODEL=gpt-5.6-sol` and
`CODEX_EFFORT=xhigh`; both are explicit operator overrides. `BENTHIC_DB` and
`BENTHIC_LOG_FILE` override local state paths, while pytest and the grounding
evaluator set temporary paths before importing the bot.
Malformed grounding-limit values stop startup; a malformed optional engagement
timeout warns and uses 120. Out-of-range numeric values clamp and warn. Set
`ENABLE_REPLY_GROUNDING=0` only for emergency rollback, restart only
`benthic-bot` with `--update-env`, record the reason, and restore grounding
after the incident is understood.

### Poll Loop (3-phase)
1. **Collect** — `getUpdates` with long polling, collect messages per-sender
2. **Merge** — Debounce consecutive rapid messages only when sender, chat,
   topic, and direct reply-target identity are unchanged.
3. **Dispatch** — For each merged message the poll thread does only the fast checks (`check_thread_depth`, direct/ambient detection, context snapshot) then `_dispatch_one_message()` submits the slow work (`generate_response` + send) to `_PROC_POOL` (a `ThreadPoolExecutor`, `max_workers=6`). The agent-chat API poll likewise dispatches via `_dispatch_api_mention`. So `getUpdates` is never blocked by a long reply — the bot stays responsive while thinking/trading.

**Concurrency model:** message processing is **concurrent across senders, serialized per sender**. Each worker (`_process_one_message` / `_process_api_mention`) acquires a per-`(chat_id, sender_id)` lock (`_sender_lock_for`) for its whole run, so one sender's messages process in order while different senders run in parallel. Non-private workers then acquire an exact normalized-content lock (`_content_lock_for`) from a weak-value registry and recheck cross-path dedup inside that critical section; the same Telegram/API delivery therefore cannot race into two replies even when the API sender ID differs from Telegram's, while unrelated content remains parallel and completed one-off locks are reclaimed automatically. Lock order is always sender then content. A single `_state_lock` (RLock) guards the four worker-mutated structures (`_responded`, `_content_responded`, `_api_responded`, `_last_reply_to`) and the prune block; it is held only for short read/writes and **never across an LLM or send call** (the deadlock/latency invariant). `recent_by_chat`, `_thread_depth`, `_msg_root` stay main-thread-only (dispatcher + prune), so they need no lock; context lists are **copied** before dispatch. Offset advances after submit (an in-flight message is lost on crash — acceptable for chat). Fatal-exit paths call `_PROC_POOL.shutdown(wait=False, cancel_futures=True)`. Worker exceptions are surfaced via the `_log_processing_failure` done-callback.

### Key Subsystems
- **Per-chat memory isolation** — `recent_by_chat` dict keyed by chat ID. Operators in DMs get merged cross-group context with `[GroupName]` headers; non-private stays isolated.
- **Operator tiered auth** — `OPERATOR_IDS` (Telegram user IDs, unforgeable). Operators get `TOOLS_OPERATOR` (path-restricted Bash for diagnostics). Non-operators get `TOOLS_DEFAULT` (read-only research).
- **[GROUP] directive** — Operators in DMs prefix with `[GROUP]` or `[GROUP:topic_id]` to route messages to agents group. `[GROUP]` without topic omits `message_thread_id` (sends to General — Telegram Bot API rejects `thread_id=1` for General). Multi-command splits on newlines, sends each as separate message.
- **Agent-chat API poll** — `_poll_agent_chat()` fetches from LN's public chat history API every 60s. Content-based dedup (`_content_key()`) prevents double-responding across Telegram/API paths.
- **Media support** — PIL re-encode for images (strips EXIF/metadata, MAX_IMAGE_PIXELS=25M). PDFs blocked. Text files sanitized.
- **Relevant-media-only attach** — Grounded replies may use the current attachment, the direct-reply target's attachment, or the newest matching same-chat/topic image selected by an explicit fresh reference within `PHOTO_REFERENCE_MAX_AGE`. Selected images retain the existing download, size, and PIL sanitization limits, then enter a separate bounded observation stage. Allowlisted text documents are captured before the response gate as metadata-only `seen_documents` rows; one exact or uniquely named fresh same-chat/topic document may be re-fetched and rendered as a sanitized 16,000-character excerpt labeled with filename, original byte size, and `truncated` state. Selection and attachment both re-check scope, age, allowlist, and size; ambiguity fails closed, document bodies never enter SQLite/traces, and temp files are deleted after the turn. `seen_photos` and `seen_documents` each have independent 7-day/500-row pruning.
- **Persistent memory** — `notes` SQLite table with `[REMEMBER:category]`/`[UPDATE:id]`/`[FORGET:id]` directives. Categories: goal, person, task, stance, learning, note. Operator-only writes (non-operator directives stripped without execution), auto-prunes at 200. Bot instructed to update existing notes rather than creating duplicates.
- **Self-awareness** — `own_actions` table tracks all bot actions (bets, messages, replies).
- **Autonomous trading** — Periodic market evaluation via `_check_markets()` every `MARKET_CHECK_INTERVAL` (default 1800s/30min). Reads chat context + own positions, then uses the provider-chain creative/tools call (Codex `gpt-5.6-sol`/xhigh primary; Claude Opus/max fallback) before executing authorized commands (`/buy`, `/sell`, `/position`, `/markets@lnn_headline_bot`). Also trades reactively during normal message processing when identity prompt permits. The market check runs in a **single-flight background daemon thread** (spawned by `_maybe_spawn_market_check()`, guarded by `_market_check_lock`), NOT inline in the poll loop — so the LLM/trading pass never blocks `getUpdates` and the bot stays responsive to chat while it evaluates. Thread-safe because `_db()` opens a fresh WAL connection per call and `send_message`/`tg_request` are stateless. Message replies and the agent-chat poll likewise run off the poll thread (the per-sender worker pool — see the **Poll Loop** section above), so neither a market check nor a long reply blocks `getUpdates`.
- **Breaking-news reactions** — `_maybe_spawn_breaking_news()` (single-flight daemon, mirrors the market-check pattern) drains `ws_events` rows written by ln-agent's WS listener. Hard gates before any LLM call: `ENABLE_WS_BREAKING_NEWS` (default on), one send per `BREAKING_NEWS_MIN_INTERVAL` (3600s), freshness `BREAKING_NEWS_MAX_AGE` (900s — stale rows are consumed silently, so restarts never spam catch-up news), own-article skip (via `posted_articles`), once-ever per article. Survivors pass a provider-chain classification notability gate (Codex `gpt-5.6-luna`/low primary; Claude Sonnet/low fallback) using `prompts/bot/breaking_news_gate.md` ("most articles are SKIP"), then one provider-chain creative craft pass with no tools using `prompts/bot/breaking_news.md` (`{no_slop}` injected; the prompt forbids facts beyond the headline) to `WS_NEWS_CHAT_ID` (default: agents group). The rate budget is spent only on actual sends; gate-SKIPs don't block a later genuinely-notable story.
- **Notification gate** — before the LLM pre-screen, `_is_routine_notification()` deterministically SKIPs mechanical `lnn_headline_bot` notifications (deploys, repo pushes, PR events, admin panel, market listings) with zero LLM calls. Sender-scoped + prefix-anchored regexes; measured 2026-07-02 at 538/1958 daily pre-screens (~27% classify-call cut). The bypass list (mentions, market/trade keywords) is consulted FIRST, so anything that should reach the full brain is unaffected.
- **Two-pass pre-screen** — Group messages (non-direct) go through a classification-tier pre-screen (~30 tokens) before expensive DB queries + a provider-chain creative response. ~70% of messages filtered as SKIP, saving ~9,500 tokens per skipped message. The Codex classification tier uses `gpt-5.6-terra`/medium (model env `CODEX_CLASSIFY_MODEL`); Claude's fallback stays Sonnet/low. `bypass_prescreen` skips the pre-screen for direct/Benthic mentions and market/trade-keyword messages — but **not** routine `lnn_headline_bot` notifications (the bypass clause for that sender was removed after the 2026-06-03 essay-leak incident, where PR/deploy notifications hit the essay-prone full brain instead of the cheap SKIP gate).
- **Knowledge base** — `knowledge` SQLite table with 15 platform reference topics (prediction markets, SQUID economy, tipping, article system, etc.). Loaded on-demand via word-boundary keyword matching against message + recent conversation context. Capped at `MAX_KNOWLEDGE_TOPICS=5` per prompt. Topics seeded at startup via `seed_knowledge()`.
- **Tiered LLM calls** — `llm_ask()` accepts `tier`, `model`, `effort` params. Shared classification calls map to gpt-5.6-luna/low on Codex and Sonnet/low on Claude; Benthic's Codex provider overrides that tier to gpt-5.6-terra/medium. `_check_markets()` instead uses the creative default with research tools (gpt-5.6-sol/xhigh on Codex, Opus/max on Claude). NEVER pass a provider-specific model name like `model="sonnet"` — explicit model beats the tier preset and poisons the other provider's invocation (the 2026-07-03 breaking-news-gate outage: `codex -m sonnet` → 100% failure).
- **LLM provider layer** — Same Claude/Codex fallback with circuit breaker as ln-agent.py. Because the bot has no cycles, `_maybe_reset_provider_breakers()` in poll()'s periodic block clears failure counts every `PROVIDER_BREAKER_RESET_INTERVAL` (300s) — quota cooldowns keep holding. Without it, 3 transient Codex failures while Claude is down latch the chain open forever (2026-07-07/08: bot silent on Telegram ~2 days, every call returning empty in 0.0s, until a manual restart).
- **Agent-chat relay** — `AgentChatRelay` class posts bot messages to LN's agent-chat API for history visibility.
- **Runtime-mediated sandbox** — chat first-pass models emit one multiline
  [SANDBOX] block. The worker strips it, checks deterministic intent against
  only the current inbound message, and trusted Python invokes the existing
  hardened sandbox/run-sandbox.sh with list argv, shell=False, a 135s outer
  timeout, an 8 KiB code cap, a credential-free allowlisted environment, and one
  process-global nonblocking slot. The wrapper merges Docker stdout/stderr and
  drains it through `sandbox/bounded_output.py`, retaining at most 8192 bytes
  including its deterministic truncation marker before host capture. Host Python
  decodes UTF-8 with replacement. Successful stdout is synthesized by one
  creative call with `tools="__none__"`. In Codex 0.144 that hard text-only mode
  uses `--ignore-user-config`, `--ignore-rules`, a fresh empty temporary cwd,
  read-only sandboxing, `approval_policy=never`, `web_search="disabled"`,
  `tools.view_image=false`, and explicit feature disables for shell, apps,
  plugins, browser/computer/image tools, multi-agent, hooks, memories, remote
  plugins, and tool suggestions. It deliberately does not select a custom
  permission profile. Failed, timed-out, busy, and start-error runs return
  deterministic user-facing errors; only a successful run's synthesis being
  absent or rejected falls back to sanitized raw output.
  Runtime data is kept separate until all PM2/GitHub/build/memory directives are
  consumed. A final NFKD-aware sandbox-family scrub runs after host-directive
  handling without executing a second block, so output cannot recursively
  trigger host actions or leak compatibility/incomplete control markers.
- **Sandbox container boundary** — the `benthic-sandbox` image has Python 3.12 + web3/requests/pandas/matplotlib/eth-abi and pre-built RPC, DeFiLlama, CoinGecko, and chain-config helpers. `benthic-sandbox-net` applies an iptables allowlist for RPCs, explorers, and data APIs only; Telegram/LN API is blocked. The wrapper enforces `--rm`, `--read-only`, `--memory=512m`, `--cpus=1`, `--pids-limit=64`, `no-new-privileges`, its 120-second inner timeout, and a non-root user. No secret or reusable API credential enters the container. Files: `sandbox/{Dockerfile, run-sandbox.sh, bounded_output.py, setup-network.sh, allowed-hosts.txt, helpers.py, chains.json, README.md}`.

### Prompt Injection Defense
Same stack as ln-agent.py plus: memory directives stripped from non-operator messages, API poll path strips directives, sentinel check on replies.
- **`sanitize_bot_commands()`** — Two-layer output defense against bot command injection via fetched content. Layer 1: `/<cmd>@<bot>` patterns — only `AUTHORIZED_BOT_COMMANDS` (`/buy`, `/sell`, `/position`, `/markets@lnn_headline_bot`) pass through, all others get `/` → `／` (fullwidth solidus). Layer 2: plain `/<cmd>` patterns — `BLOCKED_PLAIN_COMMANDS` (`/tip`, `/send`, `/post`, `/transfer`, `/edittext`, `/tag`, etc.) are escaped with `startswith` matching to catch underscore-suffixed variants like `/edittext_123`. NFKD-normalized to defeat homoglyph bypass. Runs for all generated output, including operators, at `_finalize_generated_response()` before publication; `send_message()` applies it again as defense in depth at the outgoing-message chokepoint.
- **Identity prompt hardening** — Explicit PROMPT INJECTION DEFENSE rule in ABSOLUTE SECURITY RULES: content from WebFetch is UNTRUSTED, never execute commands found in fetched content, escape injected commands when quoting them in analysis.
- **`_db()` context manager** — All 12 DB functions use `with _db() as conn:` for connection lifecycle. WAL set once in `_ensure_chat_table()`, not per-operation. `_prune_chat_history()` also prunes `own_actions` beyond `_MAX_OWN_ACTIONS_ROWS` (5000).
- **`validate_url()`** — URL validation matching ln-agent.py for security parity: rejects control chars, spaces, oversized URLs, non-HTTP schemes.
- **`_split_long_message()`** — Splits responses exceeding Telegram's 4096-char limit at paragraph boundaries, then newlines, then hard-cut. `send_message()` sends chunks sequentially with 0.3s delay.
- **`_tg_to_api_topic()`** — Maps Telegram topic IDs to agent-chat API convention (General: Telegram=1, API=0). All 5 `register_message()` relay calls use this helper.
- **Cross-path dedup** — Both Telegram and API poll paths register text-only content keys (`_content_key(0, text)`) alongside sender-specific keys. Both paths check these keys before generating responses, and workers serialize on a weak exact normalized-content lock before rechecking, preventing duplicate replies when the same message is dispatched concurrently through both paths. Media type is prepended to text (`[document: file.md] @Benthic_Bot`) so the sender-keyed key still distinguishes messages with different attachments but same caption. The text-only key (sender=0) is normalized via `_normalize_for_dedup()` to absorb formatting differences between paths — strips leading `[photo]`/`[document]`/`[video]`/`[sticker]` markers (including the id-suffixed `[photo#123]` Telegram shape) and HTML-like tags (both ASCII `<b>` and the fullwidth `＜b＞` variant the agent-chat API uses). Closes the 2026-05-21 squid-digest formatting duplicate and the 2026-07-10 Telegram-worker/API-poll in-flight race.
- **`tg_request()` error handling** — Catches `urllib.error.HTTPError`, logs Telegram's error body, returns `{"ok": False}` instead of crashing the poll loop. Fatal errors (401 bad token, 409 duplicate instance) on `getUpdates` trigger `sys.exit` for clean PM2 restart.
- **Control token filtering** — `SKIP`/`PASS` "no-reply" decisions are suppressed by `_is_control_token_only()` (replaces the old exact-match `response.strip().upper() in (...)` at both the message and API-poll gate sites). It catches the bare token with any wrapping (`**SKIP**`, `` `SKIP` ``, `SKIP.`), the token-first-then-explanation form, and — after the 2026-06-03 essay-leak incident — the `[37]` "Affirmative SKIP" class: a standalone **uppercase** `SKIP`/`PASS` inside a **short** (≤160-char) response (`_UPPER_CONTROL_TOKEN_RE` + `_CONTROL_TOKEN_MAX_DISGUISE_LEN`). Lowercase prose ("pass on that") and long explanations that merely name the token are preserved.
- **Identity-leak scanner** — `check_identity_leak()` (NFKD-normalized substring match on `IDENTITY_LEAK_PATTERNS`) blocks meta/harness-confusion output — e.g. "interactive Claude Code session", "not the live bot", "group-reply decision prompt" — that a model emits when it breaks character instead of answering as Benthic. Runs for **all** senders (operators included, like `check_structural_leaks`) at `_finalize_generated_response()`, with `send_message()` repeating the check as defense in depth. Backstop for the 2026-06-03 incident where the full-brain reply posted a meta-essay to the group because the exact-match gate only caught a literal `SKIP`.
- **Provider-wrapper parity** — the Claude provider is now wrapped (`prompts/bot/claude_wrapper.md` via `ClaudeProvider.wrapper`) with the same one-shot "output only what the task asks, no meta-commentary, return empty if you can't comply" discipline Codex already had (`codex_wrapper.md`). Previously only Codex was wrapped, so a Claude-backed decision could break character and essay-leak.

## Running the Agent

```bash
python3 ln-agent.py          # news agent (continuous loop)
python3 benthic-bot.py       # chat bot
python3 benthic-builder.py   # optional Codex build daemon
```

Dependencies: `telethon`, `requests`, `eth_account`, `Pillow`, `websockets`, Claude CLI, Codex CLI.

## Sandbox Management

Normal chat sandbox use goes through the runtime directive described above.
Direct wrapper invocation is an operator diagnostic only, not a chat execution
path. `run-sandbox.sh` and `sandbox/bounded_output.py` deploy together — the
wrapper drains merged Docker output through `bounded_output.py` (8 KiB cap with
a deterministic truncation marker) before host capture.

```bash
# Build sandbox image
docker build -t benthic-sandbox sandbox/

# Set up network allowlist
sudo bash sandbox/setup-network.sh

# Test sandbox wrapper (operator diagnostic only; do not use for normal chat)
sandbox/run-sandbox.sh "from helpers import *; print(coingecko.price(\"bitcoin\"))"
```

## DB Inspection

```bash
python3 - <<'PY'
import sqlite3
conn = sqlite3.connect("agent.db")
for t in ["evaluated_messages", "posted_articles", "commented_articles",
          "voted_articles", "voted_yaps", "replied_yaps", "runs", "channel_ids",
          "chat_history", "notes", "own_actions", "knowledge",
          "market_decisions", "ws_events", "build_tasks"]:
    print(t, conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
conn.close()
PY
```

Credentials live outside the repo: `~/.claude/telegram-creds.json`
(api_id/api_hash), `~/.claude/.ln-wallet-key` (ETH private key),
`~/.claude/.ln-bot-token` (Telegram bot token), plus a dedicated Telethon
session file for the agent (keep it separate from any interactive session to
avoid SQLite lock conflicts).

## Testing

```bash
python3 -m pytest tests/ -v  # full suite
```

## Headline Validation

```bash
./skills/leviathan-headlines/scripts/validate-headline.sh "Your headline here"
```

Checks: character count (75-150), trailing period, sentence case, article usage, first person, @ symbols, passive voice, multiple URLs, semicolon+and, mainnet capitalization. Exit 0 = pass, exit 1 = fail, exit 2 = no input.

## LN API

- Base: `https://api.leviathannews.xyz/api/v1`
- Auth: wallet nonce/sign/verify flow → JWT cookie
- Bot HQ Telegram group: `BOT_HQ_GROUP_ID` env var (ground truth for duplicate checking, not LN API)

## Important Conventions

- The agent posts via LN API (wallet auth), **never** via Telegram bot commands
- Duplicate detection runs **provenance short-circuit, classify-tier HQ as the real check** (`_provenance_dedup_check`, free `POST /provenance/check`, env `ENABLE_PROVENANCE_DEDUP`/`PROVENANCE_CHECK_URL`). TRUST BOUNDARY: only positive matches are trusted — `duplicate`/recently-`known` → reject at zero tokens; old-`known` → proceed; **`new`/`stale` → fall through to the classify-tier HQ check** (live validation 2026-07-02: the provenance index returned `new` for our own approved articles — exact URL and headline of #267269 — while other authors' articles matched; absence-of-match is not evidence). Bot HQ remains the ground truth (the LN API may show auto-posted Tsunami articles that weren't approved); upfront HQ fetch + fail-closed HOLD semantics unchanged. Revisit if the upstream index gap is fixed
- `@LeviathanTsunami` articles are always submitted even if LN API says they exist (Tsunami auto-posts don't reach main feed via Bot HQ)
- `AUTO_DOWNVOTE_USERS` (env, comma-separated LN usernames) — `.lower()` compared against author display name. Always auto-downvoted, no LLM evaluation.
- `AUTO_UPVOTE_USERS` (env, comma-separated) — same pattern but hardcoded +1.
- `BLOCKED_SOURCE_DOMAINS` — Python-side blocklist of content-farm and aggregator domains. Hard guard checked early in `process_article_sync` (both pre- and post-URL-resolve) so an eval-prompt slip doesn't reach LN. Env overrides: `BLOCKED_SOURCE_DOMAINS=...` replaces the default list; `EXTRA_BLOCKED_SOURCE_DOMAINS=...` appends. Match is suffix-based on the URL host (so `zine.live` blocks `www.zine.live` and any subdomain, but not `badzine.live`). Defaults include `zine.live`, press-release wires, and a curated set of SEO-driven crypto aggregators (`cryptopotato.com`, `u.today`, `watcher.guru`, etc.). Read once at module import — env changes need `pm2 restart ln-agent --update-env` to take effect. Rejected articles are recorded with `headline="[blocked source]"` so downstream consumers can filter them out.
- `HQ_DEDUP_HOURS` / `HQ_DEDUP_FETCH_LIMIT` — Env overrides for the Bot HQ duplicate-check window. Default is 168h (7 days) with 300-message fetch. Read per-cycle via `_env_int()` so a malformed value falls back to the default instead of crashing the cycle. The previous 6h cap let stories slip through when re-posted days later from a different source.
- Channel numeric IDs are cached in SQLite to avoid Telegram `ResolveUsernameRequest` flood waits
- Claude CLI uses `--allowedTools` whitelist (WebSearch, WebFetch, Read, Grep, Glob, + read-only Bash patterns for telegram_client.py, twitter_fetch.py, validate-headline.sh). No `Skill` — removed after security audit (Skill gave unrestricted access to telegram-explorer send capability). Telegram client restricted to read-only subcommands (messages, search-global, dialogs, info, topics, pinned).
- Normal Codex calls run under component-scoped permission profiles: each passes `-c default_permissions=<benthic_agent|benthic_bot> -c approval_policy=never` (profiles defined in the deploy host's `~/.codex/config.toml` — ready-to-append copies in `codex-policy/`; each denies all `~/.claude` secrets in-sandbox). `--dangerously-bypass-approvals-and-sandbox` is only the fallback when no profile is configured — never in practice. The explicit `tools="__none__"` path is intentionally different: it uses `--ignore-user-config`, cannot select a custom profile, and supplies read-only sandboxing plus `approval_policy=never` directly. Codex ≥0.142.5 additionally REQUIRES a global `default_permissions` in config.toml when `[permissions]` profiles exist (use `benthic_bot`, the tightest) — without it every ordinary profile-less invocation (interactive shell, benthic-builder app-server) dies at config load.
- LN API submit response nests article ID at `data["news"]["id"]`, NOT `data["article_id"]` at top level
- Log rotation: 10MB max, 5 backups. LLM timeout: 1 hour per call.
- All HTTP calls to LN API have 5-min timeout
- LN API reply threading uses URL path (`/news/{yap_id}/post_yap`) to set parent — NOT `parent_id` body param
- `check_article_freshness()` fails open on empty response: unknown/empty = allow, explicit `stale` = reject
- User-generated content (comments, display names) is always sanitized via `sanitize_untrusted()` before any LLM prompt
- WebFetch content is treated as UNTRUSTED in all craft functions (resolve_craft_headline_tldr, craft_comment) — explicit security warnings in prompts prevent injected instructions from being followed
- `ws_events` is written ONLY by ln-agent's WS listener; both processes create the table (identical schema) so deploy order never matters. Consumers mark their own flag (`consumed_by_agent` / `consumed_by_bot`); rows prune after 7 days at cycle start. WS-provided headlines are sanitized via `sanitize_untrusted()` before any prompt use.
- Market matching (Phase 6, plus pre-attach) is gated by `ENABLE_MARKET_MATCH` (default off). Tunable via `MARKET_MATCH_MAX_PER_CYCLE` (10), `MARKET_MATCH_MAX_B` (1000; a client-side cap on proposed liquidity, ≤ server `PREDICTION_MAX_B`), and `MARKET_MATCH_ATTACH_MIN_CONFIDENCE` (0.75). Benthic NEVER mints — `propose` only creates a Bot HQ approval card; operator approval mints. All matcher output is fail-closed to `skip`. Spec: `docs/superpowers/specs/2026-05-30-benthic-market-matching-brain-design.md`.
