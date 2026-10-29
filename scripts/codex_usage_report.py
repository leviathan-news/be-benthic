#!/usr/bin/env python3
"""Report Codex account rate-limit usage to Telegram.

Queries `codex app-server` (JSON-RPC over stdio) for `account/rateLimits/read`
— a backend metadata query, not a model call, so it costs zero tokens — and
posts a compact usage line to the configured Telegram chat via the chat bot.

Run it from cron every N hours on the deploy host. Also usable ad hoc:

    .venv/bin/python3 scripts/codex_usage_report.py --dry-run

PRIVACY (operator rule 2026-08-08): usage numbers and any account detail are
operator-only — send them to the operator DM; the message carries NO account
email or other PII. Never point USAGE_REPORT_CHAT_ID at a group.

Env:
    USAGE_REPORT_CHAT_ID   operator's Telegram user id (required unless --dry-run)
    BOT_TOKEN              bot token (else BOT_TOKEN_FILE,
                           default ~/.claude/.ln-bot-token)
"""

import argparse
import base64
import json
import os
import queue
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

APP_SERVER_TIMEOUT = 30
AUTH_PATH = Path.home() / ".codex" / "auth.json"
TOKEN_WARN_DAYS = 5  # warn when the access token expires within this many days


# ── app-server query ─────────────────────────────────────────────────────

def _rpc_query(methods, timeout=APP_SERVER_TIMEOUT):
    """Spawn `codex app-server`, run the handshake, and read each method once.

    Returns {method: result-or-None}. Any transport failure returns partial
    results — the caller decides what is fatal.
    """
    proc = subprocess.Popen(
        ["codex", "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True)
    out_q = queue.Queue()

    def _reader():
        for line in proc.stdout:
            out_q.put(line.strip())

    threading.Thread(target=_reader, daemon=True).start()

    def _send(msg):
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()

    def _wait(rid, deadline):
        while time.time() < deadline:
            try:
                line = out_q.get(timeout=max(0.1, deadline - time.time()))
            except queue.Empty:
                return None
            try:
                m = json.loads(line)
            except ValueError:
                continue
            if m.get("id") == rid:
                return m
        return None

    results = {}
    try:
        deadline = time.time() + timeout
        _send({"id": 1, "method": "initialize", "params": {
            "clientInfo": {"title": "Benthic Usage Monitor",
                           "name": "benthic-usage", "version": "1"},
            "capabilities": {"experimentalApi": True}}})
        if not _wait(1, deadline):
            return results
        _send({"method": "initialized", "params": {}})
        rid = 1
        for method in methods:
            rid += 1
            _send({"id": rid, "method": method, "params": {}})
            resp = _wait(rid, deadline)
            results[method] = resp.get("result") if resp else None
    finally:
        proc.kill()
    return results


# ── formatting ───────────────────────────────────────────────────────────

def _fmt_window(window, label):
    """One usage line for a rate-limit window dict, or None when absent."""
    if not window or window.get("usedPercent") is None:
        return None
    pct = window["usedPercent"]
    icon = "🔴" if pct >= 90 else ("⚠️" if pct >= 70 else "▫️")
    line = f"{icon} {label}: {pct}% used"
    resets = window.get("resetsAt")
    if resets:
        try:
            dt = datetime.fromtimestamp(int(resets), tz=timezone.utc)
            line += f" · resets {dt.strftime('%a %d %b %H:%M')} UTC"
        except (ValueError, OverflowError, OSError):
            pass
    return line


def _window_label(window):
    mins = (window or {}).get("windowDurationMins")
    if mins == 10080:
        return "weekly"
    if mins == 300:
        return "5h window"
    if mins:
        return f"{mins}-min window"
    return "window"


def build_message(rl_result):
    """Compact usage report — deliberately NO account email or other PII
    (operator rule: usage stays operator-only, and even the DM carries no
    identifiers beyond the plan tier). Raises ValueError when rate limits
    are absent."""
    rl = (rl_result or {}).get("rateLimits")
    if not rl:
        raise ValueError("no rateLimits in app-server response")
    plan = rl.get("planType") or "?"

    lines = [f"🦑 Codex usage — agent account ({plan})"]
    primary = _fmt_window(rl.get("primary"), _window_label(rl.get("primary")))
    secondary = _fmt_window(rl.get("secondary"),
                            _window_label(rl.get("secondary")))
    if primary:
        lines.append(primary)
    if secondary:
        lines.append(secondary)
    if not primary and not secondary:
        raise ValueError("rate limit windows empty")
    credits = rl.get("credits") or {}
    if credits.get("hasCredits") or credits.get("unlimited"):
        bal = "unlimited" if credits.get("unlimited") else credits.get("balance", "?")
        lines.append(f"▫️ credits: {bal}")
    if rl.get("spendControlReached"):
        lines.append("🔴 spend control reached")
    return "\n".join(lines)


def _auth_token_expiry_line(auth_path, now):
    """Warning line when the codex access token nears/passed expiry, else None.

    Decodes only the JWT `exp` claim from our own auth.json (no verification
    needed — we wrote the file). Built independently of the app-server query
    because an expired token kills that query: the warning must not die with
    the thing it warns about. Any read/parse problem returns None — the
    monitor's main job is usage reporting.
    """
    try:
        data = json.loads(Path(auth_path).read_text())
        token = (data.get("tokens") or {}).get("access_token") or ""
        _header, payload, _sig = token.split(".")
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        exp = datetime.fromtimestamp(int(claims["exp"]), tz=timezone.utc)
    except Exception:  # noqa: BLE001 — metadata-only best effort
        return None
    remaining = exp - now
    if remaining.total_seconds() <= 0:
        return (f"🔴 auth token EXPIRED {exp.strftime('%d %b %H:%M')} UTC — "
                f"run `codex login` on the host")
    if remaining > timedelta(days=TOKEN_WARN_DAYS):
        return None
    days = remaining.days
    hours = remaining.seconds // 3600
    return (f"⚠️ auth token expires in {days}d {hours}h "
            f"({exp.strftime('%d %b %H:%M')} UTC) — re-auth codex on the host "
            f"if no refresh lands first")


# ── Telegram ─────────────────────────────────────────────────────────────

def _load_token():
    token = os.environ.get("BOT_TOKEN", "").strip()
    if token:
        return token
    try:
        return Path(os.environ.get("BOT_TOKEN_FILE", "~/.claude/.ln-bot-token")
                    ).expanduser().read_text().strip()
    except OSError:
        return ""


def send_telegram(text, chat_id):
    token = _load_token()
    if not token:
        print("no bot token available", file=sys.stderr)
        return False
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=json.dumps({"chat_id": chat_id, "text": text}).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status == 200
    except Exception as e:  # noqa: BLE001 — cron job: report and exit nonzero
        # urllib errors embed the request URL, which carries the bot token.
        print(f"telegram send failed: {type(e).__name__}: "
              f"{str(e).replace(token, '<token>')}", file=sys.stderr)
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="print the report instead of sending it")
    args = parser.parse_args()

    results = _rpc_query(["account/rateLimits/read"])
    query_error = None
    try:
        message = build_message(results.get("account/rateLimits/read"))
    except ValueError as e:
        message = None
        query_error = e

    token_warning = _auth_token_expiry_line(
        AUTH_PATH, datetime.now(timezone.utc)
    )
    if message and token_warning:
        message += f"\n{token_warning}"
    elif token_warning:
        # Expired/near-expiry token likely broke the query itself — the
        # warning is the report now.
        message = (f"🦑 Codex usage — agent account\n{token_warning}\n"
                   f"🔴 usage query failed: {query_error}")
    elif not message:
        print(f"usage query failed: {query_error}", file=sys.stderr)
        return 1

    if args.dry_run:
        print(message)
        return 0
    chat_id = os.environ.get("USAGE_REPORT_CHAT_ID", "").strip()
    if not chat_id:
        print("USAGE_REPORT_CHAT_ID is required (the operator's Telegram user id)",
              file=sys.stderr)
        return 2
    return 0 if send_telegram(message, chat_id) else 1


if __name__ == "__main__":
    sys.exit(main())
