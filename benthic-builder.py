#!/usr/bin/env python3
"""benthic-builder — long-running daemon that consumes the build_tasks queue.

Architecture
------------
- Polls SQLite (agent.db) every BUILD_POLL_INTERVAL seconds for status='pending'.
- One task at a time (serial). Avoids contention, simplifies crash recovery.
- For each task: spawns Codex CLI in a per-task workdir under BUILD_ROOT/<id>/
  with full filesystem access in that dir and full network. Codex receives the brief
  plus a wrapper prompt that names the deliverables (working code, README, push to
  <BUILD_GITHUB_ORG>/<repo_name> via github_client.sh repo push).
- Hard timeout: 6 hours per task. Aim is closer to 30-90 minutes; the wall is just
  to prevent runaway loops from burning the whole window.
- On success: parses the workdir's RESULT.txt for a repo URL, posts a Telegram
  completion message threaded back to the originating chat/message.
- On failure: captures error, posts a failure message with the log path.
- On startup: sweeps stale 'running' rows whose PIDs are gone (process died across
  restart) and marks them 'failed' so the queue doesn't deadlock.

Runs as a PM2 process alongside the agent and bot. Required env:
  BUILD_GITHUB_ORG       GitHub org/user to publish repos under
  BUILD_GIT_USER_NAME    git author name for the initial commit
  BUILD_GIT_USER_EMAIL   git author email for the initial commit
"""
from __future__ import annotations

import json
import logging
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from appserver_client import AppServerClient, AppServerError

# ─── Config ──────────────────────────────────────────────────────────────────

HOME = Path.home()
# Default to the script's own directory so the daemon works out of the box.
# Override BENTHIC_BASE if you keep the agent code somewhere else.
BASE_DIR = Path(os.environ.get("BENTHIC_BASE", str(Path(__file__).parent)))
DB_FILE = Path(os.environ.get("BENTHIC_DB", str(BASE_DIR / "agent.db")))
BUILD_ROOT = Path(os.environ.get("BUILD_ROOT", "/tmp/benthic-builds"))
LOG_ROOT = Path(os.environ.get("BUILD_LOG_ROOT", "/tmp/benthic-build-logs"))
GITHUB_CLIENT = BASE_DIR / "github_client.sh"

# Where built repos are published. Required — no sensible default.
BUILD_GITHUB_ORG = os.environ.get("BUILD_GITHUB_ORG", "").strip()
# git author identity for the initial commit on each built repo.
BUILD_GIT_USER_NAME = os.environ.get("BUILD_GIT_USER_NAME", "Agent Builder")
BUILD_GIT_USER_EMAIL = os.environ.get("BUILD_GIT_USER_EMAIL", "agent@example.com")

CODEX_BIN = os.environ.get("CODEX_BIN", "codex")
# Default to the strongest reasoning tier; override via env if you need lower cost.
CODEX_MODEL = os.environ.get("CODEX_MODEL", "gpt-5.5")
CODEX_EFFORT = os.environ.get("CODEX_EFFORT", "xhigh")
TASK_TIMEOUT = int(os.environ.get("BUILD_TIMEOUT", str(6 * 3600)))     # 6 hours
POLL_INTERVAL = int(os.environ.get("BUILD_POLL_INTERVAL", "10"))       # 10 seconds
BUILD_MAX_TURNS = int(os.environ.get("BUILD_MAX_TURNS", "40"))
USE_APPSERVER = os.environ.get("BUILD_USE_APPSERVER", "1") != "0"
GOAL_TERMINAL_OK = {"complete"}
GOAL_TERMINAL_FAIL = {"blocked", "usageLimited", "paused"}  # budgetLimited cannot occur with no tokenBudget.

TG_TOKEN_FILE = HOME / ".claude/.ln-bot-token"
TG_BOT_TOKEN = TG_TOKEN_FILE.read_text().strip() if TG_TOKEN_FILE.exists() else ""

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("benthic-builder")

BUILD_ROOT.mkdir(parents=True, exist_ok=True)
LOG_ROOT.mkdir(parents=True, exist_ok=True)

# ─── DB helpers ──────────────────────────────────────────────────────────────

def db() -> sqlite3.Connection:
    c = sqlite3.connect(str(DB_FILE), timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def ensure_table() -> None:
    """Create build_tasks if it doesn't exist. The bot's _ensure_chat_table also
    creates this, but the daemon needs to be standalone-runnable without bot
    presence (e.g. fresh deployment, or builder starts before bot)."""
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS build_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            requested_by INTEGER NOT NULL DEFAULT 0,
            chat_id INTEGER NOT NULL DEFAULT 0,
            message_id INTEGER,
            request_text TEXT,
            brief TEXT NOT NULL,
            repo_name TEXT NOT NULL,
            notes TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            pid INTEGER,
            work_dir TEXT,
            log_path TEXT,
            repo_url TEXT,
            error TEXT,
            started_at TEXT,
            finished_at TEXT,
            created_at TEXT NOT NULL
        )""")
        c.commit()


def claim_next_task() -> dict | None:
    """Atomically pick the oldest pending task and mark it running. Returns the row dict.
    The dict is enriched with the new workdir/log_path/status/started_at values that
    were just written — sqlite3.Row holds a snapshot from before the UPDATE."""
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute(
            "SELECT * FROM build_tasks WHERE status = 'pending' ORDER BY id ASC LIMIT 1"
        ).fetchone()
        if not row:
            c.execute("COMMIT")
            return None
        now = datetime.now(timezone.utc).isoformat()
        workdir = BUILD_ROOT / str(row["id"])
        log_path = LOG_ROOT / f"{row['id']}.log"
        c.execute(
            """UPDATE build_tasks
               SET status='running', started_at=?, work_dir=?, log_path=?
               WHERE id = ?""",
            (now, str(workdir), str(log_path), row["id"]),
        )
        c.commit()
        result = dict(row)
        result["status"] = "running"
        result["started_at"] = now
        result["work_dir"] = str(workdir)
        result["log_path"] = str(log_path)
        return result


def record_pid(task_id: int, pid: int) -> None:
    with db() as c:
        c.execute("UPDATE build_tasks SET pid=? WHERE id = ?", (pid, task_id))
        c.commit()


def finish_task(task_id: int, status: str, repo_url: str | None = None,
                error: str | None = None) -> bool:
    """Mark a task terminal. Returns False (and changes nothing) if the task was
    already 'cancelled' — a cancel SIGTERMs the build child, whose death the builder
    would otherwise report here as 'failed', clobbering the cancellation and sending
    a false failure ping. Callers skip their completion/failure notification when
    this returns False — PR #1 finding."""
    now = datetime.now(timezone.utc).isoformat()
    with db() as c:
        cur = c.execute(
            """UPDATE build_tasks
               SET status=?, finished_at=?, repo_url=?, error=?
               WHERE id = ? AND status != 'cancelled'""",
            (status, now, repo_url, error, task_id),
        )
        c.commit()
        return cur.rowcount > 0


def sweep_orphans() -> None:
    """On startup, mark 'running' rows whose subprocess is gone as 'failed'."""
    with db() as c:
        rows = c.execute("SELECT id, pid FROM build_tasks WHERE status = 'running'").fetchall()
    for r in rows:
        pid = r["pid"]
        alive = False
        if pid:
            try:
                os.kill(pid, 0)
                alive = True
            except ProcessLookupError:
                alive = False
            except PermissionError:
                alive = True  # exists but owned by someone else
        if not alive:
            log.warning("Sweeping orphan task #%s (pid %s gone)", r["id"], pid)
            finish_task(r["id"], "failed", error="builder restarted; subprocess gone")


# ─── Telegram ────────────────────────────────────────────────────────────────

def tg_send(chat_id: int, text: str, reply_to: int | None = None) -> None:
    if not TG_BOT_TOKEN or not chat_id:
        return
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    payload: dict = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    if reply_to:
        payload["reply_to_message_id"] = reply_to
        payload["allow_sending_without_reply"] = True
    try:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            r.read()
    except urllib.error.HTTPError as e:
        log.warning("tg_send HTTP %s: %s", e.code, e.read()[:300])
    except Exception as e:
        log.warning("tg_send failed: %s", e)


# ─── Codex prompt template ──────────────────────────────────────────────────

PROMPT = """You are an autonomous senior engineer shipping a project on behalf of Benthic.
You have full write access in your workdir and full network. Be calm, methodical, and
finish without asking for permission. Aim for a working MVP, not perfection.

## PROJECT BRIEF (verbatim from the operator)

{brief}

## ENVIRONMENT

- Working directory: {workdir}  (already empty, this is your sandbox)
- Repo name: {repo_name}  (will publish as {github_org}/{repo_name})
- GitHub wrapper: {github_client}
  Use `--operator` for all calls. The wrapper handles auth and credential setup.
- DO NOT touch anything outside the working directory except the GitHub remote.
- DO NOT modify ~/.claude/, the agent's install directory, or any system path.
- DO NOT print or commit secrets, API keys, or wallet keys.
- The repo will be PUBLIC. Assume everything you push is world-readable.

## DELIVERABLES (in order)

1. Build the project under {workdir}
   - Working code: a runnable main entry point or CLI
   - Comprehensive README.md (setup, usage, architecture, brief excerpt)
   - .gitignore appropriate for the chosen language
   - Dependency manifest (requirements.txt / package.json / etc.)
   - Use .env.example for configuration; never commit real values
2. Initialize git in {workdir}
   - `git init`
   - `git -C {workdir} branch -M main`
   - `git -C {workdir} add .`
   - `git -C {workdir} -c user.name='{git_user_name}' -c user.email='{git_user_email}' commit -m 'Initial implementation: {repo_name}'`
3. Create and push the GitHub repo:
   - `{github_client} --operator repo push {repo_name} {workdir} --description "<one-line summary>"`
   The wrapper prints the canonical https URL on its last line.
4. Write the repo URL to {workdir}/RESULT.txt on its own line.
5. If anything fails unrecoverably, write a short reason to {workdir}/ERROR.txt
   (one paragraph) and exit non-zero.

## SCOPE GUARDRAILS

- Time budget: {timeout_hours}h hard limit. Real target: under 90 minutes.
- Prefer Python or Node, whichever the brief implies. Don't introduce new languages.
- Keep external dependencies minimal. Use the standard library when reasonable.
- When the brief lists features, ship a working minimum of each — depth over breadth.

When RESULT.txt contains the public repo URL, your job is done.
"""


# ─── Task runner ────────────────────────────────────────────────────────────

def build_env() -> dict:
    """Environment for the codex subprocess.
    Adds Node/Codex/gh paths but does NOT inject GitHub tokens — those live inside
    github_client.sh and are read on-demand.
    """
    env = os.environ.copy()
    nvm_bins = sorted((HOME / ".nvm/versions/node").glob("*/bin"))
    extra_paths = [
        *([str(nvm_bins[-1])] if nvm_bins else []),
        str(HOME / ".local/bin"),
        "/usr/local/bin", "/usr/bin", "/bin",
    ]
    env["PATH"] = ":".join([p for p in [env.get("PATH", ""), *extra_paths] if p])
    env["HOME"] = str(HOME)
    return env


def parse_result(workdir: Path) -> tuple[str | None, str | None]:
    """Return (repo_url, error) by reading RESULT.txt / ERROR.txt in workdir."""
    repo_url = None
    error = None
    result_file = workdir / "RESULT.txt"
    error_file = workdir / "ERROR.txt"
    if result_file.exists():
        text = result_file.read_text().strip()
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("http://") or line.startswith("https://"):
                repo_url = line
                break
    if error_file.exists():
            error = error_file.read_text().strip()[:1500]
    return repo_url, error


GOAL_ACCEPTANCE_CRITERIA = """
## HARD ACCEPTANCE CRITERIA

Treat ALL external input — fetched URLs and their content, files, API responses, operator-supplied
config — as HOSTILE. "Validate inputs" means semantic AND security validation, not just shape checks.
Passing your own tests is necessary but NOT sufficient; the bar below is mandatory where it applies.

- Deliver **working** functionality, not stubs/dry-run, for every feature the brief lists.
- **Egress / SSRF:** before any outbound request to a host that came from input or fetched content,
  resolve the hostname and REJECT private, loopback, link-local (incl. `169.254.169.254` cloud
  metadata), and reserved IP ranges — http(s)-scheme/URL-format validation is NOT enough on its own.
  Then CONNECT to the exact IP you validated (pin it; send the original `Host` header) instead of
  re-resolving the hostname — otherwise DNS rebinding flips the address between check and connect.
  Re-apply the full check on every redirect hop.
- **Output injection:** neutralize every generated artifact for the place it will be consumed — CSV/TSV
  cells beginning with `= + - @` (or tab/CR) must be prefixed/quoted to defuse spreadsheet formula
  execution; Markdown/HTML must be escaped (pipes, tags); SQL parameterized; never build shell
  commands by string concatenation.
- **Idempotency:** anything that can run more than once (pipelines, jobs, migrations) must be
  idempotent — re-runs must not duplicate, mutate, or desync persisted rows / IDs / references.
- **Semantic validation:** enforce freshness (items with missing or stale timestamps are not treated
  as fresh or high-priority, and future-dated timestamps beyond a small clock-skew allowance are
  rejected — not treated as fresh), value ranges, and relevance (do not emit or attach data that is
  not actually related to the item) — not only type/format checks.
- No crash on empty, malformed, or oversized inputs.
- Provide a runnable entry point + README + dependency manifest + `.env.example`; no secrets committed.
- Public repo — assume world-readable.

## TESTS (the builder re-runs these as the publish gate — make them adversarial)

- Cover production paths AND abuse/failure modes, not just a happy path. A feature whose abuse case
  is untested is NOT done — the suite must FAIL if the protection is removed.
- Where the bar above applies, include explicit tests for it: an SSRF attempt (a private/loopback/
  metadata URL is refused, AND a resolver that returns a public IP at check time but a private/metadata
  IP at connect time is still refused — DNS rebinding), an injection payload (formula / Markdown / SQL
  is neutralized), a re-run (idempotency holds), a future-dated item (not treated as fresh), and
  missing / garbage / oversized fields (no crash, correct rejection).

## PUBLISH CONTRACT (follow exactly)

- Write `RESULT.txt` in the working-directory root containing exactly one line of the form
  `VERIFY: <command>` -- a single shell command, runnable from the working directory, that
  installs any dependencies it needs and runs the full test suite, exiting 0 ONLY if the
  project genuinely works end to end
  (e.g. `VERIFY: pip install -r requirements.txt && python -m pytest -q`, or for a stdlib-only
  project `VERIFY: python -m unittest discover -s tests`).
- The builder re-runs that `VERIFY` command itself as the publish gate; if it is missing or
  exits non-zero, the build is rejected and nothing is published.
- Do NOT run `git push` or publish the repository yourself. Commit locally if you wish, but the
  builder publishes to GitHub only after `VERIFY` passes. Your job is to make the code real and
  to make `VERIFY` an honest end-to-end check.
"""

GOAL_OBJECTIVE_HARD_LIMIT = 4000   # app-server rejects objectives longer than this
GOAL_OBJECTIVE_BUDGET = 3800       # defensive headroom below the hard limit


def _write_brief_file(task: dict, workdir: Path) -> Path:
    """Write the full brief next to the workdir so it is never git-published.

    The generated app-server goal references this file by path. The goal agent
    has danger-full-access and can read the file, while the public project repo
    stays limited to the workdir contents.
    """
    brief_path = Path(workdir).parent / f"{task['id']}.BRIEF.md"
    brief_path.write_text(
        f"# Build brief: {task['repo_name']}\n\n"
        "## PROJECT BRIEF (verbatim from the operator)\n\n"
        f"{task['brief'].strip()}\n"
        f"{GOAL_ACCEPTANCE_CRITERIA}"
    )
    return brief_path


def _build_goal_objective(repo_name: str, brief_path: Path) -> str:
    """Build a small persistent goal objective that points at the brief file.

    The app-server caps goal objectives at GOAL_OBJECTIVE_HARD_LIMIT chars, so
    verbose operator text is stored in the sibling brief file instead of being
    inlined into the goal state.
    """
    objective = (
        f"Ship a working, tested, publishable implementation of '{repo_name}'. "
        f"The full specification and acceptance criteria are in the file {brief_path} — "
        "read that file in full first; it is the authoritative brief. Done means:\n"
        "- Every feature the brief lists is genuinely implemented and working — no stubs or dry-run.\n"
        "- All external inputs validated; no crash on empty/malformed input.\n"
        "- Tests cover success AND failure paths, not just a happy path.\n"
        "- A runnable entry point, README, dependency manifest, and .env.example exist; no secrets committed.\n"
        "- Assume the repo is PUBLIC / world-readable.\n"
        "- RESULT.txt in the working-directory root contains exactly one line `VERIFY: <command>` — a single "
        "shell command, runnable from the workdir, that installs any deps it needs and runs the full test "
        "suite, exiting 0 ONLY if the project genuinely works end to end.\n"
        "- Do NOT run git push or publish the repo yourself; the builder publishes only after it independently "
        "re-runs VERIFY and it passes. Keep working toward this goal across turns; do not redefine success "
        "around a smaller or easier task."
    )
    return objective[:GOAL_OBJECTIVE_BUDGET]


def _appserver_cmd() -> list[str]:
    """Return the codex app-server command; tests monkeypatch this to the fake."""
    return ["codex", "app-server"]


def _run_acceptance_gate(client: AppServerClient, thread_id: str, workdir: Path,
                         logwrite) -> tuple[bool, str]:
    """Run advisory app-server review output and independently execute VERIFY."""
    workdir = Path(workdir)

    try:
        rv = client.request(
            "review/start",
            {"threadId": thread_id, "target": {"type": "uncommittedChanges"}, "delivery": "inline"},
            timeout=TASK_TIMEOUT,
        )
        logwrite(f"advisory review started: reviewThreadId={rv.get('reviewThreadId')}\n")
    except Exception as e:
        logwrite(f"advisory review skipped: {e}\n")

    result_file = workdir / "RESULT.txt"
    verify_cmd = ""
    if result_file.exists():
        for line in result_file.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("VERIFY:"):
                verify_cmd = stripped[len("VERIFY:"):].strip()
                break
    if not verify_cmd:
        return False, "no VERIFY command in RESULT.txt"

    logwrite(f"=== VERIFY cmd: {verify_cmd} ===")
    try:
        result = subprocess.run(
            verify_cmd,
            shell=True,
            cwd=str(workdir),
            env=build_env(),
            text=True,
            capture_output=True,
            timeout=TASK_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        if exc.stdout:
            logwrite(str(exc.stdout).rstrip())
        if exc.stderr:
            logwrite(str(exc.stderr).rstrip())
        return False, "VERIFY failed (timeout)"

    if result.stdout:
        logwrite(result.stdout.rstrip())
    if result.stderr:
        logwrite(result.stderr.rstrip())
    if result.returncode != 0:
        return False, f"VERIFY failed (exit {result.returncode})"
    return True, ""


def _push_repo(task: dict, workdir: Path, logwrite) -> str:
    """Push the completed workdir through github_client.sh and return its https URL.

    The goal build is told NOT to publish, so the builder owns git here: make the
    workdir a committed git repo (github_client repo push requires one) regardless of
    whether the build committed anything itself."""
    def _git(*args: str, check: bool = True):
        r = subprocess.run(["git", *args], cwd=str(workdir), env=build_env(),
                           text=True, capture_output=True, timeout=300)
        if r.stdout.strip():
            logwrite(r.stdout.rstrip())
        if r.returncode != 0 and check:
            raise RuntimeError(f"git {' '.join(args)} failed (exit {r.returncode}): {r.stderr[:300]}")
        return r

    if not (workdir / ".git").is_dir():
        _git("init")
        _git("branch", "-M", "main")
    _git("add", "-A")
    has_head = subprocess.run(["git", "rev-parse", "--verify", "HEAD"], cwd=str(workdir),
                              env=build_env(), capture_output=True).returncode == 0
    if _git("status", "--porcelain", check=False).stdout.strip() or not has_head:
        _git("-c", f"user.name={BUILD_GIT_USER_NAME}", "-c", f"user.email={BUILD_GIT_USER_EMAIL}",
             "commit", "-m", f"Initial implementation: {task['repo_name']}")

    cmd = [
        str(GITHUB_CLIENT),
        "--operator",
        "repo",
        "push",
        task["repo_name"],
        str(workdir),
    ]
    logwrite(f"=== push cmd: {' '.join(cmd)} ===")
    result = subprocess.run(
        cmd,
        cwd=str(workdir),
        env=build_env(),
        text=True,
        capture_output=True,
        timeout=TASK_TIMEOUT,
    )
    if result.stdout:
        logwrite(result.stdout.rstrip())
    if result.stderr:
        logwrite(result.stderr.rstrip())
    if result.returncode != 0:
        raise RuntimeError(f"github repo push failed with exit {result.returncode}: {result.stderr[:500]}")

    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    repo_url = lines[-1] if lines else ""
    if not (repo_url.startswith("https://") or repo_url.startswith("http://")):
        raise RuntimeError("github repo push did not print an http(s) repo URL on the last line")
    return repo_url


def _startup_appserver_error(exc: Exception) -> AppServerError:
    """Wrap startup failures so run_task can fall back only before the goal loop."""
    if isinstance(exc, AppServerError):
        err = exc
    else:
        err = AppServerError(f"codex app-server startup failed: {type(exc).__name__}: {exc}")
    setattr(err, "during_startup", True)
    return err


def _run_task_goal(task: dict) -> bool:
    tid = task["id"]
    workdir = Path(task["work_dir"])
    log_path = Path(task["log_path"])
    brief_path = _write_brief_file(task, workdir)
    objective = _build_goal_objective(task["repo_name"], brief_path)
    started_at = time.time()
    state = {"latest_goal_status": None}
    turn_completed = threading.Event()
    log_lock = threading.RLock()
    client: AppServerClient | None = None

    with open(log_path, "w") as logfile:
        def logwrite(line: str) -> None:
            """Write one diagnostic line to the per-task log and flush it."""
            with log_lock:
                logfile.write(f"{line}\n")
                logfile.flush()

        def on_notification(message: dict) -> None:
            """Record app-server notifications and track the newest goal status."""
            logwrite(f"notification: {json.dumps(message, sort_keys=True)}")
            method = message.get("method")
            if method == "thread/goal/updated":
                goal = message.get("params", {}).get("goal", {})
                if isinstance(goal, dict) and goal.get("status"):
                    state["latest_goal_status"] = goal["status"]
            elif method == "turn/completed":
                # turn/start only returns an "inProgress" ack; the turn runs async and
                # ends with this notification. The loop waits on it before reading goal
                # status so a turn actually executes before we decide the next step.
                turn_completed.set()

        def remaining_timeout() -> float:
            """Return the remaining wall-clock budget for the next app-server request."""
            remaining = TASK_TIMEOUT - (time.time() - started_at)
            if remaining <= 0:
                return 0.0
            return max(0.1, min(300.0, remaining))

        def fail_goal(reason: str) -> bool:
            """Record a terminal failed state for this task and notify the requester."""
            if not finish_task(tid, "failed", error=reason):
                # Already cancelled — don't clobber the status or send a false failure ping.
                log.info("#%s already cancelled; suppressing failure notification (%s)", tid, reason)
                return True
            try:
                notify_failed(task, reason, log_path)
            except Exception:
                log.exception("#%s failed to notify goal failure", tid)
            log.warning("#%s FAILED: %s", tid, reason)
            return True

        logwrite(f"=== build_tasks #{tid} goal loop starting {datetime.now(timezone.utc).isoformat()} ===")
        logwrite(f"=== repo_name: {task['repo_name']} ===")
        logwrite(f"=== workdir:   {workdir} ===")
        logwrite(f"=== brief file: {brief_path} ===")
        logwrite(f"=== objective chars: {len(objective)} (limit {GOAL_OBJECTIVE_HARD_LIMIT}) ===")
        logwrite(f"=== timeout:   {TASK_TIMEOUT}s ===")
        logwrite(f"=== max turns: {BUILD_MAX_TURNS} ===")
        logwrite(f"=== appserver: {' '.join(_appserver_cmd())} ===")

        client = AppServerClient(
            cmd=_appserver_cmd(),
            cwd=str(workdir),
            env=build_env(),
            on_notification=on_notification,
        )
        try:
            client.start()
        except Exception as exc:
            logwrite(f"=== appserver_unavailable during startup: {type(exc).__name__}: {exc} ===")
            raise _startup_appserver_error(exc) from exc

        try:
            proc = getattr(client, "_proc", None)
            if proc is not None and getattr(proc, "pid", None):
                record_pid(tid, proc.pid)
            log.info("Starting goal build #%s: %s", tid, task["repo_name"])
            log.info("  workdir: %s", workdir)
            log.info("  log:     %s", log_path)

            thread_result = client.request(
                "thread/start",
                {
                    "model": CODEX_MODEL,
                    "cwd": str(workdir),
                    "sandbox": "danger-full-access",
                    "approvalPolicy": "never",
                    # NOTE: no "ephemeral" — the app-server rejects goals on ephemeral threads
                    # ("ephemeral thread does not support goals"). Goal builds use persisted threads.
                },
                timeout=remaining_timeout(),
            )
            thread_id = thread_result.get("thread", {}).get("id") or thread_result.get("threadId")
            if not thread_id:
                raise AppServerError(f"thread/start response missing thread id: {thread_result!r}")

            goal_result = client.request(
                "thread/goal/set",
                {"threadId": thread_id, "objective": objective},
                timeout=remaining_timeout(),
            )
            goal = goal_result.get("goal", {})
            if isinstance(goal, dict) and goal.get("status"):
                state["latest_goal_status"] = goal["status"]

            kickoff = (
                f"Read the file {brief_path} now, in full — it is the authoritative build brief "
                "and acceptance criteria. Then start building. Work until the persistent goal is "
                "complete, or report a genuine blocked state."
            )
            continue_input = (
                "Continue toward the goal. Do not stop until every acceptance criterion "
                "is met or you are genuinely blocked."
            )

            for turn_number in range(1, BUILD_MAX_TURNS + 1):
                if remaining_timeout() <= 0:
                    return fail_goal("max turns/timeout without goal completion")
                turn_input = kickoff if turn_number == 1 else continue_input
                logwrite(f"=== turn/start #{turn_number} ===")
                turn_completed.clear()
                client.request(
                    "turn/start",
                    {
                        "threadId": thread_id,
                        "input": [{"type": "text", "text": turn_input, "text_elements": []}],
                        "effort": CODEX_EFFORT,
                        "model": CODEX_MODEL,
                        "sandboxPolicy": {"type": "dangerFullAccess"},
                    },
                    timeout=remaining_timeout(),
                )
                # turn/start returned only the inProgress ack — block (up to the full
                # remaining wall-clock budget, not the 300s request cap) until the turn
                # actually finishes (turn/completed) before evaluating goal status.
                turn_wait = TASK_TIMEOUT - (time.time() - started_at)
                if turn_wait <= 0 or not turn_completed.wait(turn_wait):
                    return fail_goal("turn did not complete within timeout")
                status = state["latest_goal_status"]
                logwrite(f"=== goal status after turn #{turn_number}: {status} ===")
                if status in GOAL_TERMINAL_OK:
                    ok, reason = _run_acceptance_gate(client, thread_id, workdir, logwrite)
                    if not ok:
                        return fail_goal(reason or "acceptance gate failed")
                    repo_url = _push_repo(task, workdir, logwrite)
                    if not finish_task(tid, "done", repo_url=repo_url):
                        # Already cancelled mid-build — respect it, no "done" ping.
                        log.info("#%s already cancelled; suppressing completion notification", tid)
                        return True
                    try:
                        notify_done(task, repo_url)
                    except Exception:
                        log.exception("#%s failed to notify goal completion", tid)
                    log.info("#%s DONE: %s", tid, repo_url)
                    return True
                if status in GOAL_TERMINAL_FAIL:
                    return fail_goal(f"goal ended with terminal status: {status}")

            return fail_goal("max turns/timeout without goal completion")
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception as exc:
                    logwrite(f"=== appserver close failed: {type(exc).__name__}: {exc} ===")


def _run_task_exec_fallback(task: dict) -> bool:
    tid = task["id"]
    workdir = Path(task["work_dir"])
    log_path = Path(task["log_path"])
    error: str | None = None

    prompt = PROMPT.format(
        brief=task["brief"],
        workdir=str(workdir),
        repo_name=task["repo_name"],
        github_client=str(GITHUB_CLIENT),
        github_org=BUILD_GITHUB_ORG,
        git_user_name=BUILD_GIT_USER_NAME,
        git_user_email=BUILD_GIT_USER_EMAIL,
        timeout_hours=TASK_TIMEOUT // 3600,
    )

    log.info("Starting build #%s: %s", tid, task["repo_name"])
    log.info("  workdir: %s", workdir)
    log.info("  log:     %s", log_path)

    # Codex CLI takes reasoning effort via -c (config override) -- there is no --effort flag.
    cmd = [
        CODEX_BIN, "exec",
        "--skip-git-repo-check",
        "--ephemeral",
        "--dangerously-bypass-approvals-and-sandbox",
        "-C", str(workdir),
        "-m", CODEX_MODEL,
        "-c", f"model_reasoning_effort={CODEX_EFFORT}",
        "-",
    ]

    started_at = time.time()

    with open(log_path, "w") as logfile:
        logfile.write(f"=== build_tasks #{tid} starting {datetime.now(timezone.utc).isoformat()} ===\n")
        logfile.write(f"=== repo_name: {task['repo_name']} ===\n")
        logfile.write(f"=== workdir:   {workdir} ===\n")
        logfile.write(f"=== timeout:   {TASK_TIMEOUT}s ===\n")
        logfile.write(f"=== brief len: {len(task['brief'])} chars ===\n")
        logfile.write(f"=== cmd:       {' '.join(cmd)} ===\n\n")
        logfile.flush()

        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=logfile,
                stderr=subprocess.STDOUT,
                env=build_env(),
                cwd=str(workdir),
                text=True,
            )
            record_pid(tid, proc.pid)
            log.info("  pid:     %s", proc.pid)
            try:
                proc.communicate(input=prompt, timeout=TASK_TIMEOUT)
            except subprocess.TimeoutExpired:
                elapsed = int(time.time() - started_at)
                log.warning("#%s timed out after %ss -- sending SIGTERM", tid, elapsed)
                logfile.write(f"\n=== TIMEOUT after {elapsed}s; sending SIGTERM ===\n")
                proc.terminate()
                try:
                    proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    proc.kill()
                error = f"hard timeout after {elapsed}s"
        except FileNotFoundError as e:
            error = f"codex binary not found: {e}"
            log.error("#%s setup failed: %s", tid, error)
        except Exception as e:
            error = f"subprocess setup failed: {e}"
            log.exception("#%s setup failed", tid)

        elapsed = int(time.time() - started_at)
        logfile.write(f"\n=== finished at {datetime.now(timezone.utc).isoformat()} (elapsed: {elapsed}s) ===\n")

    repo_url, parsed_error = parse_result(workdir)
    if not error and parsed_error:
        error = parsed_error

    if repo_url and not error:
        finish_task(tid, "done", repo_url=repo_url)
        try:
            notify_done(task, repo_url)
        except Exception:
            log.exception("#%s failed to notify exec fallback completion", tid)
        log.info("#%s DONE: %s", tid, repo_url)
    else:
        if not error:
            error = "no RESULT.txt produced; check log"
        finish_task(tid, "failed", error=error)
        try:
            notify_failed(task, error, log_path)
        except Exception:
            log.exception("#%s failed to notify exec fallback failure", tid)
        log.warning("#%s FAILED: %s", tid, error)
    return True


def run_task(task: dict) -> None:
    tid = task["id"]
    workdir = Path(task["work_dir"])
    log_path = Path(task["log_path"])
    terminal_recorded = False

    try:
        workdir.mkdir(parents=True, exist_ok=True)
        # /tmp cleanup can remove an idle log root after import, so recreate
        # the parent immediately before opening this task's log file.
        log_path.parent.mkdir(parents=True, exist_ok=True)

        if USE_APPSERVER:
            try:
                terminal_recorded = _run_task_goal(task)
            except AppServerError as e:
                if getattr(e, "during_startup", False):
                    log.warning("appserver_unavailable for build #%s; using exec fallback: %s", tid, e)
                    terminal_recorded = _run_task_exec_fallback(task)
                else:
                    raise
        else:
            terminal_recorded = _run_task_exec_fallback(task)
    except Exception as e:
        # Any unexpected failure before a terminal finish_task() (e.g. the log
        # dir vanished, prompt formatting, parse error) must still drive the row
        # to 'failed' -- otherwise it stays 'running' forever (sweep_orphans only
        # runs at startup) and the status reader falsely reports "still building".
        error = f"builder task failed unexpectedly: {type(e).__name__}: {e}"
        log.exception("#%s failed before terminal task state", tid)
        if terminal_recorded:
            return
        try:
            finish_task(tid, "failed", error=error)
            terminal_recorded = True
        except Exception:
            log.exception("#%s failed to record terminal failure state", tid)
        try:
            notify_failed(task, error, log_path)
        except Exception:
            log.exception("#%s failed to notify terminal failure", tid)


def notify_done(task: dict, repo_url: str) -> None:
    text = (
        f"✅ Build #{task['id']} done — {task['repo_name']}\n"
        f"Repo: {repo_url}\n\n"
        "Operator: sign + POST submit when ready (signature for the relevant bounty, "
        "or skip if this wasn't a bounty)."
    )
    tg_send(task["chat_id"], text, reply_to=task.get("message_id"))


def notify_failed(task: dict, error: str, log_path: Path) -> None:
    text = (
        f"❌ Build #{task['id']} failed — {task['repo_name']}\n"
        f"Reason: {error[:500]}\n"
        f"Log: {log_path}"
    )
    tg_send(task["chat_id"], text, reply_to=task.get("message_id"))


# ─── Main loop ──────────────────────────────────────────────────────────────

def main() -> None:
    if not BUILD_GITHUB_ORG:
        sys.exit("ERROR: BUILD_GITHUB_ORG env var is required — set it to the "
                 "GitHub org/user repos should be published under.")
    log.info("benthic-builder starting")
    log.info("  DB:            %s", DB_FILE)
    log.info("  build root:    %s", BUILD_ROOT)
    log.info("  log root:      %s", LOG_ROOT)
    log.info("  poll interval: %ss", POLL_INTERVAL)
    log.info("  task timeout:  %ss (%sh)", TASK_TIMEOUT, TASK_TIMEOUT // 3600)
    log.info("  codex:         %s (model: %s, effort: %s)", CODEX_BIN, CODEX_MODEL, CODEX_EFFORT)
    log.info("  github org:    %s", BUILD_GITHUB_ORG)
    log.info("  git author:    %s <%s>", BUILD_GIT_USER_NAME, BUILD_GIT_USER_EMAIL)
    ensure_table()
    sweep_orphans()

    while True:
        try:
            task = claim_next_task()
            if task:
                run_task(task)
            else:
                time.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            log.info("shutting down (KeyboardInterrupt)")
            break
        except Exception:
            log.exception("unexpected error in main loop")
            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
