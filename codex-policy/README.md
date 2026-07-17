# Codex Permission Lockdown

Server-side Codex policy for the Benthic bot, agent, and API provider calls.
Replaces `--dangerously-bypass-approvals-and-sandbox` (full shell on untrusted
input) with restricted permission profiles + an execpolicy ruleset, so a
prompt-injection cannot read or exfiltrate secrets while real tools keep working.
benthic-builder is intentionally excluded (it uses `codex app-server`, not
`providers.py`, and still needs full access).

## Install on the server

1. Append the `[permissions.*]` blocks from `permissions.toml` to `~/.codex/config.toml`.
2. Copy `rules/benthic.rules` to `~/.codex/rules/benthic.rules`. NOTE: execpolicy
   rules are host-global (every Codex run loads them unless `--ignore-rules`); they
   only allow the wrapper tools and forbid destructive/secret-read commands, so
   they're safe for builder/interactive use too.
3. Do NOT set a global `default_permissions`. `providers.py` selects a profile per
   call via `-c default_permissions=<name>` (+ `-c approval_policy=never`); with no
   global default, builder/interactive Codex keep their normal behavior.
4. Activation is the `providers.py` change (deployed with the app): `CodexProvider`
   emits `-c default_permissions=<profile> -c approval_policy=never` instead of the
   bypass flag, and ships a secret-stripped env to Codex. Reversal = set
   `permission_profile=None` (instant return to bypass).

## How it works (validated empirically)

- **Every sensitive `~/.claude` file is denied to in-sandbox commands** in every
  profile (the four profiles are filesystem-identical). A `deny` blocks reads at the
  OS sandbox level. `~/.claude/plugins` stays readable so tool scripts can be found.
- **execpolicy `allow` runs the matched command OUTSIDE the sandbox**, where the deny
  doesn't apply. The three credential-reading wrappers — `github_client.sh`,
  `telegram_client.py` (read-only subcommands), `twitter_fetch.py` — are allowed, so
  they read their own creds (and Telethon writes its session) when invoked **directly**.
- **`curl` is NOT allowed** → it runs in-sandbox: it can still fetch arbitrary URLs
  (grounding; network enabled) but cannot read any denied secret.
- **Compound commands run in-sandbox.** Anything with `$(...)`, a pipe, or `bash -c`
  is matched as `bash` (not allow-listed) → in-sandbox. So an injected
  `curl ?x=$(cat <secret>)` or `github_client.sh issue create --body "$(cat <secret>)"`
  has its secret-read run in-sandbox → denied. Only *bare* tool commands with literal
  args run outside the sandbox, and those can't embed a secret read.
- **Codex env is secret-stripped** in `providers.py` (`*KEY*`/`*TOKEN*`/`*SECRET*`/… 
  removed, except `ETHERSCAN_API_KEY`), closing the env-exfil channel.

Net: the model can never obtain a secret (all in-sandbox reads denied; the
outside-sandbox tools read only their own hardcoded creds and don't echo them), so
there is nothing to exfiltrate; grounding and tools still work.

## Validation evidence

`cat <denied cred>` in-sandbox → BLOCKED; `curl --data-binary @<denied cred>` →
BLOCKED; `curl <api>` grounding → 200; bare `github_client.sh issue` → OK (reads
token outside sandbox); bare telegram `dialogs` → OK (reads+writes session);
`execpolicy check` → wallet/cred reads + `rm -rf`/`ssh` = forbidden, tool wrappers =
allow. Fail-open check: a missing/mistyped profile makes Codex hard-error
(`default_permissions requires a [permissions] table`) → fails closed (chain falls
back to Claude), never unrestricted.

## Operational requirement & residuals

- **Invoke the cred tools directly.** A credential-reading tool reads its cred only
  when run as a bare command; piping/wrapping it drops it in-sandbox where it
  fails-closed (safe, but the tool call fails). The bot/agent codex wrappers instruct
  Codex to invoke `github_client.sh`/`telegram_client.py`/`twitter_fetch.py` directly.
- **Operator-only powers are NOT granted via Codex.** execpolicy is host-global /
  sender-agnostic, so operator tools (`--operator` github, pm2, benthic-build) are
  deliberately not allow-listed — they would otherwise be usable by any sender (the
  rate-limited `--user` github path still works for everyone). `benthic-build` was
  moved to the bot's sender-aware Python layer: Codex emits a `[BUILD:repo]…[/BUILD]`
  directive (operator-only; stripped-without-executing for non-operators) and the bot
  runs benthic-build outside the sandbox. pm2 and `--operator` github via Codex are
  degraded — operators run those directly.
- **execpolicy is defense-in-depth** (known Unicode-confusable prefix-match bypass,
  gh #13095) — the filesystem profile is the hard boundary.

## Profiles

`benthic_bot`, `benthic_bot_operator`, `benthic_agent`, `benthic_api` — currently
filesystem-identical (deny all sensitive `~/.claude` files + `~/.ssh`/`~/.aws`/
`~/.gnupg`/`~/.netrc`; read `/path/to/agent-dir`; write `/tmp`; network enabled). Kept
as separate names so `providers.py` can diverge them later (e.g. per-component egress).
