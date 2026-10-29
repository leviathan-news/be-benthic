#!/usr/bin/env python3
"""Opt-in original buyer-conversation replay with real RPC and captured sends.

Runs the fixed collector in an existing remote sandbox, not arbitrary model
code. Telegram, trading, build, and other host-action boundaries stay blocked.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.eval_reply_replays import ReplayRuntime, CHAT_ID, message_from_event, stage_name
import onchain_evidence
from prompt_loader import _cache, _PROMPT_DIR

POOL_URL = "https://www.geckoterminal.com/fraxtal/pools/0x277fa53c8a53c880e0625c92c92a62a9f60f3f04"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-providers", action="store_true")
    parser.add_argument("--sandbox-host", required=True)
    # Agent checkout on the sandbox host (holds sandbox/run-sandbox.sh and .venv).
    parser.add_argument("--remote-agent-dir", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--turn", choices=("all", "explicit"), default="all")
    args = parser.parse_args()
    if not args.live_providers or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,100}", args.sandbox_host):
        parser.error("requires --live-providers and a valid operator-supplied SSH host")
    if not re.fullmatch(r"/[A-Za-z0-9._/-]{1,200}", args.remote_agent_dir):
        parser.error("--remote-agent-dir must be an absolute path of [A-Za-z0-9._/-]")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for path in _PROMPT_DIR.rglob("*.md"):
        _cache[str(path.relative_to(_PROMPT_DIR).with_suffix(""))] = path.read_text()
    events = [
        {"id": 18001, "text": "whos buying? " + POOL_URL},
        {"id": 18003, "text": "How about the last months?"},
        {"id": 18005, "text": "That's not what I asked, who's buying since August 1?"},
    ]
    if args.turn == "explicit":
        events = [{"id": 18001, "text": "Who's buying since August 1? " + POOL_URL}]
    results, stages, requests, scans, permitted_codes = [], [], [], [], []
    original_builder = onchain_evidence.worker_code
    def build(request):
        code = original_builder(request)
        permitted_codes.append(code)
        requests.append(request)
        return code
    with tempfile.TemporaryDirectory(prefix="onchain-replay-") as temp, patch.object(onchain_evidence, "worker_code", build):
        with ReplayRuntime(Path(temp), live_reader=True) as runtime:
            bot = runtime.bot
            original_provider = bot._replay_original_provider
            def provider(prompt, validator, **kwargs):
                stage = "onchain_plan" if prompt.startswith("Select a read-only on-chain") else stage_name(prompt)
                receipt = original_provider(prompt, validator=validator, **kwargs)
                row = {"stage": stage, "provider": receipt.provider if receipt else None,
                       "output": receipt.text if receipt else None}
                stages.append(row)
                (args.output_dir / "stages.json").write_text(json.dumps(stages, indent=2) + "\n")
                return receipt
            bot._provider_chain.ask_validated = provider
            def sandbox(code, message, sender):
                if code not in permitted_codes:
                    raise RuntimeError("replay blocked non-collector sandbox execution")
                permitted_codes.remove(code)
                remote = ('import sys,subprocess; p=subprocess.run(['
                          + json.dumps(args.remote_agent_dir + "/sandbox/run-sandbox.sh")
                          + ',sys.stdin.read()],'
                          'capture_output=True,text=True,timeout=130);'
                          'print(p.stdout,end="");print(p.stderr,end="",file=sys.stderr);sys.exit(p.returncode)')
                started = time.monotonic()
                completed = subprocess.run(
                    ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", args.sandbox_host,
                     shlex.quote(args.remote_agent_dir + "/.venv/bin/python3") + " -c "
                     + shlex.quote(remote)],
                    input=code, text=True, capture_output=True, timeout=140,
                )
                if completed.returncode:
                    raise RuntimeError("read-only collector failed: " + completed.stderr[:500])
                value = onchain_evidence.validate_result(completed.stdout, requests[-1])
                scans.append(value)
                (args.output_dir / "scans.json").write_text(json.dumps(scans, indent=2) + "\n")
                return bot.SandboxRunResult("ok", completed.stdout, 0, time.monotonic() - started)
            bot._run_sandbox = sandbox
            recent = []
            for event in events:
                message = message_from_event(event, now=int(time.time()))
                if recent:
                    if not runtime.sent:
                        raise RuntimeError("continuation lacks a captured bot reply")
                    message["reply_to_message"] = runtime.sent[-1]
                before, scan_before = len(runtime.sent), len(scans)
                bot._process_one_message(message, recent, is_direct=True, is_private=False)
                answer = "\n".join(row["text"] for row in runtime.sent[before:])
                with bot._db(row_factory=True) as db:
                    trace = db.execute("SELECT disposition,failure_reason,verifier_result FROM reply_grounding_traces "
                                       "WHERE chat_id=? AND message_id=? ORDER BY created_at DESC LIMIT 1",
                                       (CHAT_ID, event["id"])).fetchone()
                trace = dict(trace) if trace else {}
                scanned = scans[scan_before:]
                passed = bool(answer and trace.get("verifier_result") == "pass" and scanned
                              and scanned[-1]["status"] in {"ok", "partial"})
                if "months" in event["text"] or "August" in event["text"]:
                    # These real records were independently checked before the
                    # replay. The model never receives the expected answer here.
                    passed = passed and "ccbf" in answer.lower() and any(
                        text in answer.lower().replace(",", "") for text in ("2316762", "2.317", "2.316", "2.32m", "september 8", "sept 8", "sep 8"))
                result = {"message": event["text"], "passed": bool(passed), "answer": answer,
                          "trace": trace, "actual_scans": len(scanned), "external_publications": 0}
                results.append(result)
                print(json.dumps(result), flush=True)
                (args.output_dir / "results.json").write_text(json.dumps(results, indent=2) + "\n")
                recent.append(message)
    return 0 if all(row["passed"] for row in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
