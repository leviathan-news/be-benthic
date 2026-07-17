#!/usr/bin/env python3
"""Scriptable fake codex app-server speaking JSONL over stdin/stdout.

Used by the builder tests so they never invoke real Codex. Behavior is driven
by a JSON script file whose path is in $FAKE_APPSERVER_SCRIPT:

{
  "thread_id": "t1",
  # one entry consumed per turn/start; each sets the goal status reported
  # (via a thread/goal/updated notification) after that turn.
  "turns": [ {"goal_status": "active"}, {"goal_status": "complete"} ],
  "review_findings": [],          # list of {"severity": "...", "title": "..."}
  "crash_after_turn": null,       # int -> process exits 1 after N turn/start calls
  "sleep_methods": {}             # optional: {"method": seconds} to delay a reply
}

Protocol framing matches lib/app-server.mjs: newline-delimited JSON. Requests
carry an integer "id" and get a {"id", "result"} reply; notifications are
{"method", "params"} with no id.
"""
import json
import os
import sys
import time


def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def goal_obj(thread_id, status):
    # Mirrors the v2 ThreadGoal shape from the generated protocol types.
    return {
        "threadId": thread_id,
        "objective": "obj",
        "status": status,
        "tokenBudget": None,
        "tokensUsed": 0,
        "timeUsedSeconds": 0,
        "createdAt": 0,
        "updatedAt": 0,
    }


def thread_obj(thread_id):
    # Provides the Thread.id field used by the real ThreadStartResponse.
    return {"id": thread_id}


def turn_obj(turn_id):
    # Provides the minimal Turn fields consumed by code paths that inspect review/start output.
    return {
        "id": turn_id,
        "items": [],
        "itemsView": "full",
        "status": "completed",
        "error": None,
        "startedAt": 0,
        "completedAt": 0,
        "durationMs": 0,
    }


def main():
    script = json.load(open(os.environ["FAKE_APPSERVER_SCRIPT"]))
    tid = script.get("thread_id", "t1")
    turns = list(script.get("turns", []))
    sleeps = script.get("sleep_methods", {})
    turn_i = 0

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        method = msg.get("method")
        mid = msg.get("id")

        # Optional artificial delay to exercise client request timeouts.
        if method in sleeps:
            time.sleep(float(sleeps[method]))

        if method == "initialize":
            emit({"id": mid, "result": {}})
        elif method == "initialized":
            pass  # notification, no reply
        elif method == "thread/start":
            emit({"id": mid, "result": {"thread": thread_obj(tid), "threadId": tid}})
        elif method == "thread/goal/set":
            emit({"id": mid, "result": {"goal": goal_obj(tid, "active")}})
        elif method == "thread/goal/get":
            st = turns[min(turn_i, len(turns) - 1)].get("goal_status", "active") if turns else "active"
            emit({"id": mid, "result": {"goal": goal_obj(tid, st)}})
        elif method == "turn/start":
            spec = turns[turn_i] if turn_i < len(turns) else {"goal_status": "complete"}
            turn_i += 1
            # Emit the goal status update + turn completion BEFORE the response,
            # so tests verify the client handles interleaved notifications.
            emit({"method": "thread/goal/updated",
                  "params": {"threadId": tid, "turnId": f"turn{turn_i}", "goal": goal_obj(tid, spec["goal_status"])}})
            emit({"method": "turn/completed", "params": {"threadId": tid, "turnId": f"turn{turn_i}"}})
            emit({"id": mid, "result": {"turn": turn_obj(f"turn{turn_i}")}})
            if script.get("crash_after_turn") == turn_i:
                sys.exit(1)
        elif method == "review/start":
            emit({"id": mid, "result": {
                "turn": turn_obj("review1"),
                "reviewThreadId": tid,
                "findings": script.get("review_findings", []),
            }})
        elif method == "turn/interrupt":
            emit({"id": mid, "result": {}})
        else:
            # Unknown request: reply with empty result so the client doesn't hang.
            if mid is not None:
                emit({"id": mid, "result": {}})


if __name__ == "__main__":
    main()
