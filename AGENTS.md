# AGENTS.md

## Leviathan News Agent State

- `ln-agent.py` is the automated Leviathan News worker. It runs under PM2.
  With Jev news push enabled, `run_agent()` only performs source
  cursor catch-up and durable queue insertion, followed by a one-hour wait.
  It does not authenticate to LN, run models, post, or perform engagement.
  Jev evaluation, recovery, and publication workers run continuously.
- `LN_TELEGRAM_SESSION` selects the worker's Telethon session (legacy default
  `~/.claude/agent_session.session`). Give the worker its own independently
  authorized session; never clone its authorization into Explorer or another
  process (copies compete for Telegram updates even under different filenames). Between hourly scans, Jev mode refreshes Telegram update
  delivery and requests gap recovery every 30 seconds, with a ten-second request
  bound and explicit flood-wait backoff. Recovered updates use the existing
  source handlers and durable unique keys.
- One independent worker serializes hourly LN engagement reconciliation,
  WebSocket-triggered vote/comment mini-passes, and reply checks every ten
  minutes. This prevents overlapping reply generation and works even when
  Telegram sources are quiet or disconnected. Successful reconciliation
  acknowledges only the queued events whose articles it actually processed;
  events received during that pass remain pending. Intervals run from pass
  completion. The existing comment budget, reply dedup, article caps, and
  optional market-matching gate still apply. Market matching remains part of
  engagement reconciliation when enabled.
- Maintenance runs at startup and then every 24 hours. It prunes seven-day
  WebSocket/source records and terminal Jev jobs; idle evaluators do not run
  cleanup queries. `ENGAGEMENT_RECONCILE_INTERVAL` defaults to `3600` and
  `REPLY_CHECK_INTERVAL` to `600`, each with a minimum of 60 seconds.
- Without Jev real-time news, the legacy loop retains its six phases: read
  Telegram, evaluate/deduplicate, post, vote/comment, reply detection, and
  flag-gated market matching.
- Phase 3 resolves the primary URL and crafts only the headline. Successful
  ordinary and Tsunami submissions are persisted and upvoted but never receive
  an automatic TLDR, promotion note, or other self-comment.
  In Jev news mode, one researched Sol/medium call (180-second cap, no soul)
  checks freshness, resolves the source, and crafts the headline. It replaces
  separate Luna freshness and Sol/xhigh headline calls. Stale stories get the
  existing terminal marker; unreadable sources and invalid output stay retryable.
  An unknown date remains eligible only with a readable supporting source.
- Phase 4 unsolicited analysis comments apply only to third-party, positively
  voted articles that already have a non-Benthic yap. A durable SQLite rolling
  budget allows at most `ARTICLE_COMMENT_MAX_PER_24H` comments per 24 hours
  (default `3`, bounded `0..24`, where `0` disables them). The creative stage
  may return exact `SKIP` when it has no useful edge. Full and mini-passes share
  the same policy and reservation table; votes and direct replies are unchanged.
- White-label routing defaults: `BENTHIC_NEWS_CHANNEL` has no default, so
  channel posting is off until it is set (the bot must be a channel admin).
  `ENABLE_WIRE_SUBMISSIONS` defaults to `0` because LN staff-gates wire rows.
  With neither, only stories at or above `LN_SUBMIT_MIN_HEAT` (default `8`)
  are submitted to LN, and colder stories are skipped before any craft call;
  set `LN_SUBMIT_MIN_HEAT=1` to submit every newsworthy story. Private
  channels/groups are read from `ID_SOURCES` (JSON id -> label), and
  `PUSH_EXCLUDED_SOURCES` defaults to empty.
- LLM routing is provider-based. `PROVIDER_ORDER` defaults to `codex` in the
  bot and news worker. Claude is retired from production; there is no
  automatic provider fallback. Benthic grounded chat composition and repair use
  the `chat_reply` tier, defaulting to gpt-6-astra/xhigh via `CODEX_REPLY_MODEL`.
  Research and other creative defaults remain gpt-6-sol/xhigh.
  Benthic and shared ln-agent classification calls use gpt-6-luna/medium under
  Codex unless explicitly routed to Jev by `JEV_TASKS`. Benthic's independent grounded
  reply verifier uses gpt-6-luna/high. Legacy provider adapters require explicit
  configuration to participate in the chain.
- Jev uses TypeSafe's fixed System One endpoint with pinned model `jev-1.13.0`.
  `JEV_TASKS` independently enables `news`, `votes`, `duplicates`,
  `market_prefilter`, `reply_gate`, `breaking_news`, and `engagement`; all seven
  are off by default (empty `JEV_TASKS`) and need a TypeSafe API key. Jev returns bounded decisions,
  URL choices, and heat scores. Creative prose, researched freshness checks,
  source/context selection, sentinels, and grounded verification retain their
  existing providers. There is no automatic Jev-to-Codex fallback.
- With `ENABLE_SOURCE_PUSH=1` and `news` in `JEV_TASKS`, independent evaluation
  and publication workers run throughout source catch-up and its sleep. All
  configured sources are eligible for push unless listed in
  `PUSH_EXCLUDED_SOURCES` (default empty). Existing message
  prefilters still apply. Both `NewMessage` and `MessageEdited` subscribe to
  the same source map. A recent caption edit can enqueue the first eligible
  version of a previously unqueued message; the existing channel/message unique
  key prevents edit replays. Edits use the original message's three-day age
  bound, not the edit time. `jev_news_jobs` durably separates evaluation from
  publication, preserves accepted candidates across restart/failure, and retries
  with backoff capped at 300 seconds. Failed evaluation never becomes rejection.
  News evaluation uses at most four concurrent requests; publication is serial
  and retains source checks and creative headline writing. Source candidates
  older than three days expire. Source dedup is part of Jev news evaluation;
  the separate `duplicates` flag controls the Bot HQ check.
- Jev news policy v4 lives in `prompts/jev/news.json`. One request asks separate
  crypto-relevance, concrete-event, exclusion, per-URL eligibility, and heat
  questions. Code combines the decisions; multiple eligible URLs do not compete
  for a single Choice probability. Recognized publishers use exact hostname or
  dot-delimited subdomain matching, then still require a relevant article link.
  An exact transaction path on a configured explorer identifies incident
  evidence for triage, never proof of an exploit or its amount. Unknown sources
  that fail only source eligibility, plus bare or short context-poor links,
  enter a durable `recover` stage. Two source readers run separately from the
  four fast evaluators; ready results are persisted as they finish.
  `jev_sources.py` reuses the bounded HTTP and exact-X readers: at most two
  witnessed URLs, ten seconds per read within a 20-second collection window,
  512 KiB per HTTP response and 1 MiB total allowance. No links from fetched
  text are followed. Source excerpts are sanitized and capped at 8,000 bytes.
  Jev rechecks relevance, event, exclusions, and source support on fetched text,
  with separate unsafe-action and historical-recap gates. Source fame is not
  a substitute for reading the report. Source failures retry without a negative
  label; expired recovery becomes `unavailable`. After at least three failed
  recovery attempts, a read blocked solely by HTTP 401/403 or required sign-in
  also becomes `unavailable`. Mixed failures, rate limits, and outages retain
  the normal retry budget; a readable accepted second URL still wins.
  Cross-queue dedup serializes
  comparison and persistence and includes newer accepted source IDs awaiting
  publication. Completed jobs cannot suppress candidates through unpublished
  rejection text; final dedup uses successful publication history.
  Routine exchange custody, stablecoin treasury transfers, and automatic
  transfers involving exploiter-labelled addresses remain excluded unless
  there is a substantive new development. New freezes, negotiated returns,
  attribution findings, active exploits, reserve attestations, and material
  foundation/company treasury actions retain their editorial eligibility.
  Metadata logs record the policy hash, input hash, component probabilities,
  and rejection reason, without source text or URLs. Comment votes distinguish
  neutral acknowledgment from spam or empty cheerleading.
- Jev news publication repeats self-dedup after the final headline is crafted.
  It compares that headline independently with each successfully published
  headline from the last 24 hours, in batches of 25 questions. Duplicate and
  blocked-source markers do not enter this history, and the window is not
  truncated to the latest 25 posts. A process-wide lock covers the final URL
  recheck, semantic comparison, publication and ledger save. Classifier failure
  holds the candidate for retry. This catches translated source hints and
  simultaneous copies; genuine later developments remain separate events.
  Jev news mode bypasses the legacy word-overlap rejection, which confuses
  shared venues/topics with the same event. The legacy path retains its guard.
- A stale freshness verdict writes a terminal URL marker with no story hint,
  destination, or publication receipt. This ends push retries without adding
  an unpublished story to semantic dedup history. If source resolution finds
  an already handled or blocked canonical URL, the original alias also gets
  a terminal marker without publication metadata, so that job does not repeat
  freshness and creative work. Source-retrieval and
  publication failures use `NEWS` logs; publication exceptions record stage,
  exception class, HTTP status when available, and source ID without exception
  bodies.
- Model questions receive a hash-bound runtime receipt of configured reply
  providers before composition. Configuration proves the primary model and any
  explicitly configured fallback, while claims about a particular reply require
  its actual composer receipt. General model questions use runtime evidence
  without web research.
- Operator build requests route Telegram completion/failure pings through per-call `BENTHIC_BUILD_*` environment variables injected by `benthic-bot.py`; `bin/benthic-build` treats those env vars as authoritative over any `--chat`, `--message`, or `--user` flags.
- Goal-driven builder tasks write the full operator brief plus acceptance criteria to a sibling `{task_id}.BRIEF.md` file outside the generated project workdir, then send the Codex app-server a short objective that references that file.
- Provider-related environment variables:
  - `PROVIDER_ORDER` (default `codex`)
  - `CLAUDE_BIN` (legacy adapter only)
  - `CODEX_BIN`
  - `CODEX_MODEL`
  - `CODEX_REPLY_MODEL` (Benthic chat composition and repair only; default `gpt-6-astra`)
  - `CODEX_EFFORT` (default `xhigh`)
  - `CODEX_CLASSIFY_MODEL`
  - `JEV_TASKS` (comma-separated task names; empty disables Jev)
  - `JEV_API_KEY_FILE` (default `jev.key` beside `jev_news.py`)
  - `TYPESAFE_API_KEY` (optional key override; never log its value)
  - `CLAUDE_LIMIT_COOLDOWN` (legacy adapter only)
  - `BENTHIC_ENGAGEMENT_TIMEOUT` (default `120`, clamped to `30..300`)
  - `ARTICLE_COMMENT_MAX_PER_24H` (default `3`, clamped to `0..24`; `0` disables unsolicited article comments)
- Benthic local-state path overrides:
  - `BENTHIC_DB` (default `agent.db` beside `benthic-bot.py`)
  - `BENTHIC_LOG_FILE` (default `benthic.log` beside `benthic-bot.py`)
- Market-matching environment variables (Phase 6, plus pre-attach, default off):
  - `ENABLE_MARKET_MATCH` (default `0`)
  - `MARKET_MATCH_MAX_PER_CYCLE` (default `10`)
  - `MARKET_MATCH_MAX_B` (default `1000`)
  - `MARKET_MATCH_ATTACH_MIN_CONFIDENCE` (default `0.75`)
  - Depends on squid-bot PR #380 deployed; the agent's LN user must be `is_staff=True`.
- Benthic chat publication is evidence-bounded when
  `ENABLE_REPLY_GROUNDING=1`: usefulness gate -> typed evidence collection ->
  tools-disabled composition -> tools-disabled verification -> at most one
  tools-disabled repair and re-verification -> existing security/directive
  finalizer -> send. Unsupported ambient output is silent; direct mentions
  and replies receive deterministic uncertainty or provider-error wording.
  Each independent Luna/high verifier call may retry the same Codex primary
  once after a non-empty contract-invalid response, using the same prompt,
  resolved request, and 300-second per-call cap. Empty output, valid pass/fail
  verdicts, an opened primary breaker, and fallback contract failures are not
  retried; every other provider-chain call keeps the zero-retry default.
- Telegram and agent-chat API ingress both route through the common
  `generate_response()` evidence-grounding path before their existing
  `_finalize_generated_response()` and publication seams.
- Direct requests that combine frozen prediction markets with resolution,
  outcome, or settlement intent use a dedicated trusted runtime adapter. It
  reads at most ten `status=frozen` rows from the fixed LN API endpoint under a
  15-second and 256-KiB cap, rejects malformed, duplicate, or non-frozen rows,
  and adds only validated IDs, questions, status, and timestamps as a hash-bound
  `runtime:frozen_markets` receipt. Source discovery may use one root per listed
  market up to the existing ten-request turn cap, but the runtime receipt proves
  only market identity and criteria, never the outcome. Tools-disabled
  composition and Luna verification remain mandatory; replies recommend
  `YES`, `NO`, or `REFUND` with actual source URLs and never emit or execute
  resolution admin commands. Lookup failures use dedicated runtime wording,
  while a trusted empty snapshot skips unrelated web research.
- Group ingress treats anchored natural vocatives such as `Benthic ...`,
  `Benthic, ...`, `Benthic: ...`, and `Hey, Benthic bot ...` as direct while
  rejecting incidental later mentions, filenames, and URLs. A leading
  decorative prefix may contain at most eight Unicode symbols, their variation
  marks, or allowlisted ornamental punctuation and must be separated from the
  anchored vocative by whitespace; ordinary words, handles, dot prefixes, and
  overlong decoration remain non-direct. The engagement gate receives a
  separate sanitized 300-character direct-reply target block, including its
  deterministic media marker. With `engagement` in `JEV_TASKS`, Jev evaluates
  structured current-message, reply, and history fields; deterministic direct
  and media overrides remain. Otherwise the Luna/medium call uses the bounded
  `BENTHIC_ENGAGEMENT_TIMEOUT` instead of a fixed 30-second cap.
- Focal URLs normally come from the current message or its direct reply target.
  A bare URL and the same sender's immediately following short caption remain
  one source task across polling batches when separated by at most 30 seconds,
  with matching chat/topic and no new direct reply target.
  A tools-disabled Luna classifier may resolve a current caption or correction
  to one of at most ten witnessed human link messages from the same chat and
  forum topic within 30 minutes. It selects only an eligible message ID;
  collection repeats scope, age, author-type, and presence checks, preserves
  the current task, and adds the referenced message as R2 plus fetched focal
  content. Ambiguity fails closed. Source-recovery replies require a claim
  supported by the fetched source, followed by independent semantic verification;
  apology-only replies receive the existing single repair attempt. An unreadable
  recovered page gets an explicit runtime limitation, without asking to resend
  the known link. Unselected history URLs remain background. Ordinary Telegram
  reply threads do not partition conversation history; actual forums retain
  topic isolation, including persisted legacy rows. Prior
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
  with a UTF-8-safe 48,000-byte sanitized body cap plus filename, byte size,
  and `truncated` state, and deleted after the turn. `truncated=false` supports
  whole-document work; `truncated=true` remains excerpt-only. The independent
  attachment seam repeats age, type, size, and scope checks; ambiguity asks for
  an exact reply or filename. Evidence fitting drops background, conversation,
  then runtime receipts before selected media or the current task. Successful
  grounded Telegram sends persist metadata-only `reply_document_sources`
  receipts from actual outgoing message IDs to exact trace-bound document
  messages. A direct reply to that authenticated Benthic message may reuse its
  unique same-chat/topic document within the seven-day media-retention window;
  generic document inference remains limited to 30 minutes. Selection and
  rehydration independently repeat retention, scope, allowlist, size, and
  download-type checks, while missing or conflicting receipts fail closed.
- Source URLs canonicalize before dispatch and cache lookup. X/Twitter root
  hosts and every dot-delimited subdomain, plus standard ports, trailing-dot
  hosts, and `/photo/N` or `/video/N` suffixes, route only through exact status
  extraction; generic HTTP rejects them on initial and redirect hops.
  `Background:` and `Background only:` are the only supported explicit
  background labels and must occupy a line with exactly one URL; ambiguous
  labels fail closed. Declared background URLs reserve their turn-local slot
  even when unavailable, and a cached typed failure prevents a second request.
- One explicit private Telegram reference chain per direct turn may resolve
  at most three messages through the installed Telegram Explorer helper.
  Each message may name only one next private message; ambiguous branches and
  cycles fail closed, with cycles compared by chat/message identity even across
  URL aliases. Every lookup is restricted to the
  current allowed group and topic, with an exact message-ID range, 15-second
  deadline, 64-KiB helper-output cap, and one process slot. Ordinary non-forum
  reply threads do not constrain lookup to that thread; forum chats retain
  topic isolation, including the normalized agent-chat API ingress. Background
  research and DMs cannot use this authenticated reader. Every linked message
  and its first-level public source URLs enter flat focal evidence under the
  shared focal-URL, request, byte, and collection-deadline budgets. Private links
  are never fetched through generic HTTP or expanded from public-page content.
  Missing, out-of-scope, ambiguous, cyclic, or over-depth message references
  produce `telegram_message_unavailable` and ask for a direct reply or pasted
  text. Composition, repair, and verification retain the original task while
  allowing an explicitly referenced question to be supplied by that message.
- CSS `var()` evaluation supports bounded nested fallbacks. A visible fallback
  is used only when the property is not declared in the inspected page or inline
  styles; inherited, cyclic, malformed, or CSS-wide custom-property values stay
  conservative. Explicit `display:none` and other existing hiding checks remain.
- Exact HTTPS YouTube watch, `youtu.be`, mobile-watch, and Shorts URLs
  canonicalize to one eleven-character video ID and route before generic HTTP.
  Only public manual or autogenerated captions are supported: selection prefers
  manual English, generated English, manual original language, then generated
  original language, with no translation, cookies, account, audio download, or
  speech-to-text path. The pinned helper permits only direct YouTube/Innertube
  HTTPS GET/POST traffic, uses a 30-second maximum deadline, 2 MiB aggregate
  response cap, 65,536-byte stdout cap, 256 MiB address-space cap, 30-second CPU
  cap, and a dedicated two-child process limit with confirmed reaping. Its
  47,000-byte caption body plus metadata may use a 48,000-byte evidence allowance;
  `truncated=true` supports only the stated caption interval. Direct terminal
  failures use `youtube_captions_unavailable` and say: "I couldn't access public
  captions for that video, so I can't summarize it reliably." Ambient failures
  remain silent. Logs omit video IDs and traces hash their YouTube references.
  Run a caption preflight from the deploy host before relying on it; no proxy
  fallback is permitted. Both `defusedxml==0.7.1` and
  `youtube-transcript-api==1.2.4` are hash-pinned in
  `requirements/youtube-transcript.lock`; rollback is
  `.venv/bin/python3 -m pip uninstall -y youtube-transcript-api defusedxml` plus
  restoration of the runtime-file archive when both packages were newly added.
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
  repair, and the Luna verifier reject stale grievances, old self-critiques,
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
  `GROUNDING_MAX_SOURCE_BYTES=4194304`,
  `GROUNDING_SOURCE_COLLECTION_TIMEOUT=960`,
  `GROUNDING_MAX_EVIDENCE_BYTES=65536`, `GROUNDING_FETCH_TIMEOUT=15`,
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

- LN coverage comparisons use dated, explicitly filtered public archive samples.
  Discovery requests three rows per period from `/api/v1/news/search/`; the
  ordinary feed ignores date filters. `ln_archive.py` validates public status,
  row dates and pagination, then projects bounded JSON with sampling limits and
  attributed comment excerpts. The transport still charges the complete body.
  Own-activity receipts identify LN article comments by venue, date and link.
- Codex 0.157 uses the `view_image` feature gate; `tools.view_image` is ignored.
  Isolated stages disable that tool while selected sanitized images still use
  exact `-i` attachments. Successful CLI exits with no answer log metadata and
  a prompt hash, not echoed input; research logs the matching chat/message IDs.
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
- The periodic autonomous market gate does not run a full LLM analysis when the
  only snapshot change is that markets left the open set. It refreshes the
  cached open snapshot with `MARKETS_CLOSED` and returns. New markets, stale
  analysis, or a material price move in a remaining open market still trigger
  the existing researched trading pass.
- Telegram and agent-chat API workers serialize non-private duplicate deliveries
  through a weak exact normalized-content lock registry, then recheck dedup
  before generation.
  Keep lock order sender then content so the two ingress paths cannot race into
  duplicate replies when their sender IDs differ.
