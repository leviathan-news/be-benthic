# AGENTS.md

## Leviathan News Agent State

- `ln-agent.py` is the automated Leviathan News worker. It runs as a PM2 process on the dev server and always sleeps 30 minutes after each completed cycle.
- The runtime loop has six phases: read Telegram, evaluate/deduplicate, post, vote/comment, reply detection, and (flag-gated) market matching.
- LLM routing is provider-based. Codex is primary by default and Claude is the
  automatic fallback. Benthic chat creative calls use gpt-5.6-sol/xhigh and
  Benthic classification calls use gpt-5.6-terra/medium under Codex. Shared
  ln-agent classification remains gpt-5.6-luna/low. Claude's explicit no-tier
  creative fallback is Opus/max; classification remains Sonnet/low.
- Claude quota/limit failures open a cooldown window controlled by `CLAUDE_LIMIT_COOLDOWN` (default `21600` seconds). During that window the agent skips Claude and uses Codex directly.
- Operator build requests route Telegram completion/failure pings through per-call `BENTHIC_BUILD_*` environment variables injected by `benthic-bot.py`; `bin/benthic-build` treats those env vars as authoritative over any `--chat`, `--message`, or `--user` flags.
- Goal-driven builder tasks write the full operator brief plus acceptance criteria to a sibling `{task_id}.BRIEF.md` file outside the generated project workdir, then send the Codex app-server a short objective that references that file.
- Provider-related environment variables:
  - `CLAUDE_BIN`
  - `CODEX_BIN`
  - `CODEX_MODEL`
  - `CODEX_EFFORT` (default `xhigh`)
  - `CODEX_CLASSIFY_MODEL`
  - `CLAUDE_LIMIT_COOLDOWN`
  - `BENTHIC_ENGAGEMENT_TIMEOUT` (default `120`, clamped to `30..300`)
- Benthic local-state path overrides:
  - `BENTHIC_DB` (default `agent.db` beside `benthic-bot.py`)
  - `BENTHIC_LOG_FILE` (default `benthic.log` beside `benthic-bot.py`)
- Market-matching environment variables (Phase 6, plus pre-attach, default off):
  - `ENABLE_MARKET_MATCH` (default `0`)
  - `MARKET_MATCH_MAX_PER_CYCLE` (default `10`)
  - `MARKET_MATCH_MAX_B` (default `1000`)
  - `MARKET_MATCH_ATTACH_MIN_CONFIDENCE` (default `0.75`)
  - Depends on squid-bot PR #380 deployed; benthic's LN user must be `is_staff=True`.
- Benthic chat publication is evidence-bounded when
  `ENABLE_REPLY_GROUNDING=1`: usefulness gate -> typed evidence collection ->
  tools-disabled composition -> tools-disabled verification -> at most one
  tools-disabled repair and re-verification -> existing security/directive
  finalizer -> send. Unsupported ambient output is silent; direct mentions
  and replies receive deterministic uncertainty or provider-error wording.
- Telegram and agent-chat API ingress both route through the common
  `generate_response()` evidence-grounding path before their existing
  `_finalize_generated_response()` and publication seams.
- Group ingress treats anchored natural vocatives such as `Benthic ...`,
  `Benthic, ...`, `Benthic: ...`, and `Hey, Benthic bot ...` as direct while
  rejecting incidental later mentions, filenames, and URLs. The engagement
  gate receives a separate sanitized 300-character direct-reply target block,
  including its deterministic media marker, and its Terra/medium call uses the
  bounded `BENTHIC_ENGAGEMENT_TIMEOUT` instead of a fixed 30-second cap.
- Focal URLs come only from the current message or its direct reply target.
  Same-author history and older conversation links remain background. Prior
  images are eligible only when directly replied to or explicitly referenced
  within `PHOTO_REFERENCE_MAX_AGE`; selected sanitized images are observed in
  a separate bounded stage before tools-disabled composition. Codex receives
  only exact validated `-i` attachments with `view_image` disabled; Claude gets
  exact `Read(path)` rules. Media evidence keeps separate sanitized-artifact
  and observation-text hashes. The immutable final producer receipt is rebuilt
  after repair and supplied to the final verifier, so repaired prose and traces
  cannot retain stale original-composer attribution.
- Allowlisted text attachments are indexed in metadata-only `seen_documents`
  rows before the response gate; bodies never enter SQLite. One exact or
  uniquely named recent same-chat/topic document may be re-fetched, rendered
  as a sanitized 16,000-character excerpt with filename, byte size, and
  `truncated` state, and deleted after the turn. The independent attachment
  seam repeats age, type, size, and scope checks; ambiguity asks for an exact
  reply or filename. `truncated=true` never supports a whole-document claim.
- Source URLs canonicalize before dispatch and cache lookup. X/Twitter root
  hosts and every dot-delimited subdomain, plus standard ports, trailing-dot
  hosts, and `/photo/N` or `/video/N` suffixes, route only through exact status
  extraction; generic HTTP rejects them on initial and redirect hops.
  `Background:` and `Background only:` are the only supported explicit
  background labels and must occupy a line with exactly one URL; ambiguous
  labels fail closed. Declared background URLs reserve their turn-local slot
  even when unavailable, and a cached typed failure prevents a second request.
- Generic web evidence uses `SafeHttpFetcher`'s direct, proxy-free transport.
  Each hop is prevalidated as public and pinned to that IP, HTTPS keeps normal
  hostname-based TLS verification, and only ports 80/443 are allowed. The
  complete fetch is bounded to at most three redirects, 15 seconds, and 1 MiB
  per response. RSS, Atom, and generic XML responses use bounded text-node
  extraction after DTD and entity declarations are rejected; malformed XML
  fails closed. Only `display` and `visibility` declarations form the
  supported CSS visibility subset: unsupported inline declarations suppress
  their element, unsupported page CSS rejects the page, and semantic hidden
  containers include closed `dialog` elements. Non-rendered `head`, `title`,
  and meta-description content never enters visible body evidence. A turn
  additionally caps focal URLs, total source requests, and total fetched
  response bytes; the shared ledger is debited inside HTTP/X capture even when
  decoding, visibility parsing, JSON parsing, or focal validation later fails.
  Each transport keeps its own debit counter, so delayed bytes from a failed
  capture cannot satisfy the next source's charge.
  Cache hits remain usable after the collection deadline because they spend no
  additional request, byte, or transport time. One absolute collection
  deadline includes research discovery, provider fallback, Claude retries, and
  source transport. The default 960-second deadline gives a fresh Sol/xhigh
  discovery call up to 900 seconds while retaining at least 30 seconds for one
  configured fallback and 30 seconds for trusted source fetching. Earlier
  focal work contracts those windows inside the same absolute deadline.
  With three unreserved background roots, discovery may return six canonical
  candidates. General research retains the existing preferred-source and
  ordinary-HTML scheduler. A current-message token-market request instead
  activates an immutable exact-asset plan with EVM network, normalized contract
  address, and identity, market, and thesis candidate roles. Roles are untrusted
  scheduling hints: trusted validators bind Blockscout token metadata and
  GeckoTerminal token, pool, and 4H OHLCV JSON to the exact contract before a
  lane is covered. The 4H adapter requires
  `/ohlcv/hour?aggregate=4&limit=24`, finite six-field candles, and strictly
  monotonic timestamps. Evidence rendering preserves only those two allowlisted
  OHLCV query fields so tools-disabled composition sees that the `hour` path is
  a 4H aggregate; every other source query and fragment remains stripped.
  Machine-readable
  identity and market candidates run before social material; social sources
  cannot consume their reserved capacity, and a thesis is admitted only after
  both required lanes are covered and the fetched text names the exact
  contract. Failed, malformed, mismatched, or source-ref-duplicate candidates
  spend the shared ledger but do not consume accepted root capacity; explicit
  background URLs keep their reservation.
  Discovery returns typed `research_unavailable`,
  `research_sources_unavailable`, or `source_collection_timeout` metadata;
  those reasons retain precedence over the grounded composer-abstention
  fallback `research_evidence_insufficient`. Incomplete market identity/data
  lanes are withheld from composition rather than padded with adjacent social
  context. A verified reply records no failure. Composer abstentions leave
  model reply and claims fields empty, so direct failure wording remains
  runtime-owned. Composer and repair answer a materially useful supported
  subset and describe checked-source limits in natural public language. A
  deterministic pre-verifier gate rejects internal grounding terms such as
  evidence IDs, bundle/support-matrix mechanics, verifier narration, and the
  former supplied-evidence formula; one normal repair remains available before
  terminal failure. A repair output receives a narrow deterministic wording
  normalization for those internal phrases before the final verifier; factual
  values and claim bindings are unchanged, and any residual leak still fails.
  The independent verifier also requires token claims and
  theses to match the same exact contract/network and requires the actual URL
  when prose says a thesis was found. Unscoped or world-level absence,
  exhaustive-search, attribution, quantity, and factual-premise claims remain
  fail-closed. The final direct-action block
  repeats the supported-subset precedence: a material gap is not terminal when
  another requested part has a useful supported answer, and `uncertain` is
  reserved for turns with no materially useful supported subset. Composer and
  repair terminal blocks use that same no-useful-subset condition; later prompt
  text must not reinterpret a partial evidence gap as terminal. Creative stages
  may omit irrelevant evidence, keep inference labels clause-local, and use gap
  disclosures only for requested parts; repair deletes unsupported introduced
  relationships instead of manufacturing a non-connection claim.
  DNS work and Twitter child/reaper work each use a
  process-wide finite slot cap; killed Twitter children are nonblockingly reaped
  or handed to a bounded daemon cleanup, and capacity is returned only after
  child reaping is confirmed.
- The verifier permits meaning-preserving paraphrases and does not relax actor,
  source, quantity, or attribution checks. A chronological older/newer claim is
  accepted only when the cited evidence timestamp is ordered earlier/later than
  the relevant comparison-source timestamp. If either timestamp is absent or
  ordering does not support the claim, verification fails. Public prose need
  not print timestamps. Opinion fixtures must contain the referent their
  opinion evaluates. M0 and R1 define the current requested task: runtime
  receipts and older context may support but cannot redefine it. Composition,
  repair, and the Terra verifier reject stale grievances, old self-critiques,
  or provider failures used in place of the requested action, while preserving
  materially useful supported-subset precedence. `chat_history.timestamp`
  remains ingestion time while the
  nullable `event_time` column carries canonical UTC source time; legacy or
  malformed event times provide no chronology evidence. Explicitly unavailable
  API event times never fall back to Unix epoch, and a merged turn's latest
  message ID carries the latest fragment's matching event time.
- `reply_grounding_traces` stores IDs, source refs, hashes, lengths, fetch
  status, provider/model metadata, verdict, and disposition only. It never
  stores message/page text, provider output, prompts, credentials, local
  paths, or Telegram file IDs. Defaults: `ENABLE_REPLY_GROUNDING=1`,
  `GROUNDING_MAX_BACKGROUND_SOURCES=3`,
  `GROUNDING_MAX_FOCAL_URLS=8`, `GROUNDING_MAX_SOURCE_REQUESTS=10`,
  `GROUNDING_MAX_SOURCE_BYTES=2097152`,
  `GROUNDING_SOURCE_COLLECTION_TIMEOUT=960`,
  `GROUNDING_MAX_EVIDENCE_BYTES=24000`, `GROUNDING_FETCH_TIMEOUT=15`,
  `GROUNDING_TRACE_RETENTION_DAYS=14`, `PHOTO_REFERENCE_MAX_AGE=1800`,
  `BENTHIC_ENGAGEMENT_TIMEOUT=120` (bounded to `30..300`).
  The default research allocator gives Sol up to 900 seconds, preserves 30
  seconds for one provider fallback, and preserves 30 seconds for trusted
  source fetching. The timeout is a cap, so successful calls return early.
  Operators may configure the total only within 960 through 1,800 seconds.
  `ENABLE_REPLY_GROUNDING=0` is an emergency rollback to the legacy response
  path and must not be treated as a normal operating mode.
- Consecutive Telegram fragments merge only when sender, chat, topic, and
  direct reply target are identical. Both ingress paths use the same empty
  generation normalization: direct turns receive neutral clarification and
  ambient turns remain silent; provider failure wording requires a typed
  grounding failure.

## Operational Notes

- Bot HQ (configured via `BOT_HQ_GROUP_ID`) remains the ground truth for duplicate detection.
- User-generated content is always sanitized before entering any LLM prompt.
- Reply generation still uses injection checks plus an independent sentinel decision before posting.
- Benthic bot output gates suppress standalone leading `SKIP`/`PASS` control
  tokens and tight identity/meta-essay leaks at the publication security seam,
  `_finalize_generated_response()`. `send_message()` repeats the identity and bot-
  command checks as defense in depth. Claude calls use the same one-shot output-
  discipline contract as Codex.
- Routine `lnn_headline_bot` notifications no longer bypass the cheap group pre-screen; direct mentions and market/trade keyword notifications still do.
- Chat sandbox work is runtime-mediated. A first-pass [SANDBOX] block is parsed
  and intent-gated from the current user message, then trusted bot Python runs
  sandbox/run-sandbox.sh under a one-job lock and minimal environment. Successful
  output receives one `tools="__none__"` creative synthesis pass. For Codex this
  is a hard text-only invocation: user config and rules are ignored, tool-bearing
  features are disabled, web search is `web_search="disabled"`, execution is
  read-only with no approvals, and the cwd is a fresh empty temporary directory.
  Docker stdout/stderr is drained through `sandbox/bounded_output.py` and capped
  at 8192 bytes before host capture. No reusable API credential enters arbitrary
  sandbox code, runtime output is never parsed as a host directive, and model
  shells never receive Docker access.
- Credential-free Ethereum sandbox reads use PublicNode and 1RPC. Every RPC
  hostname in `sandbox/chains.json` must also appear in
  `sandbox/allowed-hosts.txt`; endpoint changes require rebuilding the image and
  rerunning `sandbox/setup-network.sh` to refresh static DNS and egress rules.
- Exact-token research treats the trusted `market` lane as a coverage gate, not
  a one-source quota. When spare evidence-root capacity remains after reserving
  every missing required lane, the collector may retain one source per distinct
  recognized machine shape, such as 4H OHLCV and pool liquidity; untrusted role
  labels alone never qualify a supplemental source. Exact Gecko token-pool
  responses are projected to three contract-bound pools before evidence fitting,
  while the source-byte ledger still charges the complete transport response.
- Telegram and agent-chat API workers serialize non-private duplicate deliveries
  through a weak exact normalized-content lock registry, then recheck dedup
  before generation.
  Keep lock order sender then content so the two ingress paths cannot race into
  duplicate replies when their sender IDs differ.
