# AGENTS.md

## Leviathan News Agent State

- `ln-agent.py` is the automated Leviathan News worker. It runs as a PM2 process and always sleeps a configurable interval after each completed cycle.
- The runtime loop has five phases: read Telegram, evaluate/deduplicate, post, vote/comment, and reply detection.
- LLM routing is now provider-based:
  - Claude CLI is the primary provider.
  - Codex CLI is the automatic fallback when Claude fails or hits quota/rate-limit style errors.
  - Reply sentinel checks prefer Claude Sonnet and fall back to Codex through the same provider layer.
- Claude quota/limit failures open a cooldown window controlled by `CLAUDE_LIMIT_COOLDOWN` (default `21600` seconds). During that window the agent skips Claude and uses Codex directly.
- Provider-related environment variables:
  - `CLAUDE_BIN`
  - `CODEX_BIN`
  - `CODEX_MODEL`
  - `CLAUDE_LIMIT_COOLDOWN`

## Operational Notes

- Bot HQ (configured via `BOT_HQ_GROUP_ID`) remains the ground truth for duplicate detection.
- User-generated content is always sanitized before entering any LLM prompt.
- Reply generation still uses injection checks plus an independent sentinel decision before posting.
