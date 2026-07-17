from __future__ import annotations

import importlib.util
import json
import os
import sys
import types
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parent.parent
BUILDER_PATH = REPO / "benthic-builder.py"
FAKE_APPSERVER = REPO / "tests" / "fake_appserver.py"


def load_builder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """Load the hyphenated builder module with all runtime paths owned by pytest."""
    base = tmp_path / "base"
    build_root = tmp_path / "builds"
    log_root = tmp_path / "logs"
    db_file = tmp_path / "agent.db"
    base.mkdir(parents=True, exist_ok=True)

    # The builder reads these environment variables at import time, so each test
    # imports a fresh module after pointing global paths at the temporary tree.
    monkeypatch.setenv("BENTHIC_BASE", str(base))
    monkeypatch.setenv("BUILD_ROOT", str(build_root))
    monkeypatch.setenv("BUILD_LOG_ROOT", str(log_root))
    monkeypatch.setenv("BENTHIC_DB", str(db_file))
    monkeypatch.setenv("BUILD_USE_APPSERVER", "1")
    monkeypatch.setenv("BUILD_MAX_TURNS", "5")

    spec = importlib.util.spec_from_file_location(
        f"benthic_builder_goal_test_{uuid.uuid4().hex}",
        BUILDER_PATH,
    )
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # Keep timeout short in tests while still exercising the wall-clock guard.
    mod.TASK_TIMEOUT = 5
    monkeypatch.setattr(mod, "tg_send", lambda *args, **kwargs: None)
    return mod


def write_fake_script(tmp_path: Path, script: dict) -> Path:
    """Write the fake app-server behavior script consumed through the environment."""
    path = tmp_path / f"fake-appserver-{uuid.uuid4().hex}.json"
    path.write_text(json.dumps(script))
    return path


def insert_and_claim_task(mod: types.ModuleType) -> dict:
    """Insert one pending build row and return the claimed running task snapshot."""
    now = datetime.now(timezone.utc).isoformat()
    with mod.db() as conn:
        conn.execute(
            """INSERT INTO build_tasks (
                   requested_by, chat_id, message_id, request_text, brief,
                   repo_name, notes, status, created_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                0,
                0,
                None,
                "please build a thing",
                "Build a thing.",
                "thing",
                None,
                "pending",
                now,
            ),
        )
        conn.commit()
    task = mod.claim_next_task()
    assert task is not None
    return task


def insert_and_claim_task_with_brief(
    mod: types.ModuleType,
    brief: str,
    repo_name: str = "thing",
) -> dict:
    """Insert a pending task with a caller-controlled brief and claim it."""
    now = datetime.now(timezone.utc).isoformat()
    with mod.db() as conn:
        conn.execute(
            """INSERT INTO build_tasks (
                   requested_by, chat_id, message_id, request_text, brief,
                   repo_name, notes, status, created_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                0,
                0,
                None,
                "please build a thing",
                brief,
                repo_name,
                None,
                "pending",
                now,
            ),
        )
        conn.commit()
    task = mod.claim_next_task()
    assert task is not None
    return task


def fetch_task(mod: types.ModuleType, task_id: int) -> dict:
    """Return the mutable task fields asserted by the goal-loop tests."""
    with mod.db() as conn:
        row = conn.execute(
            "SELECT status, error, repo_url FROM build_tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
    assert row is not None
    return dict(row)


def point_at_fake_appserver(
    mod: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    script_path: Path,
) -> None:
    """Route AppServerClient startup to the scriptable local fake process."""
    monkeypatch.setenv("FAKE_APPSERVER_SCRIPT", str(script_path))
    monkeypatch.setattr(mod, "_appserver_cmd", lambda: [sys.executable, str(FAKE_APPSERVER)])


def test_goal_complete_runs_gate_and_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A complete goal runs the gate, pushes, and records the repo URL as done."""
    mod = load_builder(tmp_path, monkeypatch)
    mod.ensure_table()
    script = write_fake_script(
        tmp_path,
        {
            "thread_id": "t1",
            "turns": [{"goal_status": "active"}, {"goal_status": "complete"}],
            "review_findings": [],
        },
    )
    point_at_fake_appserver(mod, monkeypatch, script)
    monkeypatch.setattr(mod, "_run_acceptance_gate", lambda *args, **kwargs: (True, ""))
    monkeypatch.setattr(
        mod,
        "_push_repo",
        lambda *args, **kwargs: "https://github.com/ExampleOrg/thing",
    )

    task = insert_and_claim_task(mod)
    mod.run_task(task)

    row = fetch_task(mod, task["id"])
    assert row["status"] == "done"
    assert row["repo_url"] == "https://github.com/ExampleOrg/thing"
    assert row["error"] is None


def test_goal_uses_generated_protocol_shapes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The goal runner sends the generated app-server v2 request shapes."""
    mod = load_builder(tmp_path, monkeypatch)
    mod.ensure_table()
    requests: list[tuple[str, dict]] = []

    class C:
        """Capture app-server requests while simulating a completed goal turn."""

        def __init__(self, *args, **kwargs) -> None:
            self.on_notification = kwargs["on_notification"]

        def start(self) -> None:
            pass

        def request(self, method: str, params: dict, timeout: float | None = None) -> dict:
            requests.append((method, params))
            if method == "thread/start":
                return {"thread": {"id": "t1"}, "threadId": "legacy-t1"}
            if method == "thread/goal/set":
                return {"goal": {"status": "active"}}
            if method == "turn/start":
                self.on_notification(
                    {
                        "method": "thread/goal/updated",
                        "params": {"threadId": "t1", "goal": {"status": "complete"}},
                    }
                )
                self.on_notification(
                    {"method": "turn/completed", "params": {"threadId": "t1", "turnId": "turn1"}}
                )
                return {"turn": {"id": "turn1"}}
            return {}

        def close(self) -> None:
            pass

    monkeypatch.setattr(mod, "AppServerClient", C)
    monkeypatch.setattr(mod, "_run_acceptance_gate", lambda *args, **kwargs: (True, ""))
    monkeypatch.setattr(
        mod,
        "_push_repo",
        lambda *args, **kwargs: "https://github.com/ExampleOrg/thing",
    )

    task = insert_and_claim_task(mod)
    mod._run_task_goal(task)

    thread_start = next(params for method, params in requests if method == "thread/start")
    goal_set = next(params for method, params in requests if method == "thread/goal/set")
    turn_start = next(params for method, params in requests if method == "turn/start")
    assert thread_start == {
        "model": mod.CODEX_MODEL,
        "cwd": task["work_dir"],
        "sandbox": "danger-full-access",
        "approvalPolicy": "never",
    }

    objective = goal_set["objective"]
    assert goal_set["threadId"] == "t1"
    assert len(objective) <= 4000
    assert "thing" in objective
    assert "VERIFY:" in objective
    assert ".BRIEF.md" in objective
    assert "Build a thing." not in objective

    kickoff = turn_start["input"][0]["text"]
    assert turn_start["threadId"] == "t1"
    assert turn_start["effort"] == mod.CODEX_EFFORT
    assert turn_start["model"] == mod.CODEX_MODEL
    assert turn_start["sandboxPolicy"] == {"type": "dangerFullAccess"}
    assert turn_start["input"][0]["type"] == "text"
    assert turn_start["input"][0]["text_elements"] == []
    assert "Read the file" in kickoff
    assert ".BRIEF.md" in kickoff
    assert "Build a thing." not in kickoff


def test_goal_objective_stays_under_4000_for_large_brief(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Large briefs are written to disk while the app-server objective stays small."""
    mod = load_builder(tmp_path, monkeypatch)
    mod.ensure_table()
    requests: list[tuple[str, dict]] = []

    class C:
        """Capture app-server requests while immediately completing the first turn."""

        def __init__(self, *args, **kwargs) -> None:
            self.on_notification = kwargs["on_notification"]

        def start(self) -> None:
            pass

        def request(self, method: str, params: dict, timeout: float | None = None) -> dict:
            requests.append((method, params))
            if method == "thread/start":
                return {"thread": {"id": "t1"}}
            if method == "thread/goal/set":
                return {"goal": {"status": "active"}}
            if method == "turn/start":
                self.on_notification(
                    {
                        "method": "thread/goal/updated",
                        "params": {"threadId": "t1", "goal": {"status": "complete"}},
                    }
                )
                self.on_notification(
                    {"method": "turn/completed", "params": {"threadId": "t1", "turnId": "turn1"}}
                )
                return {"turn": {"id": "turn1"}}
            return {}

        def close(self) -> None:
            pass

    monkeypatch.setattr(mod, "AppServerClient", C)
    monkeypatch.setattr(mod, "_run_acceptance_gate", lambda *args, **kwargs: (True, ""))
    monkeypatch.setattr(
        mod,
        "_push_repo",
        lambda *args, **kwargs: "https://github.com/ExampleOrg/thing",
    )

    big_brief = "Build a platform. " + "FEATURE spec line. " * 1800
    task = insert_and_claim_task_with_brief(mod, big_brief)
    mod._run_task_goal(task)

    goal_set = next(params for method, params in requests if method == "thread/goal/set")
    brief_file = Path(task["work_dir"]).parent / f"{task['id']}.BRIEF.md"
    row = fetch_task(mod, task["id"])

    assert len(goal_set["objective"]) <= 4000
    assert brief_file.exists()
    brief_text = brief_file.read_text()
    assert big_brief.strip() in brief_text
    assert "VERIFY:" in brief_text
    assert row["status"] == "done"


def test_goal_blocked_fails_no_push(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blocked goal records failure and never invokes the repo push path."""
    mod = load_builder(tmp_path, monkeypatch)
    mod.ensure_table()
    script = write_fake_script(
        tmp_path,
        {"thread_id": "t1", "turns": [{"goal_status": "blocked"}]},
    )
    point_at_fake_appserver(mod, monkeypatch, script)
    pushed: list[object] = []
    monkeypatch.setattr(mod, "_push_repo", lambda *args, **kwargs: pushed.append(args))

    task = insert_and_claim_task(mod)
    mod.run_task(task)

    row = fetch_task(mod, task["id"])
    assert row["status"] == "failed"
    assert "blocked" in row["error"]
    assert pushed == []


def test_appserver_crash_no_zombie(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mid-loop app-server crash is terminal failure, not a silent running row."""
    mod = load_builder(tmp_path, monkeypatch)
    mod.ensure_table()
    script = write_fake_script(
        tmp_path,
        {
            "thread_id": "t1",
            "turns": [{"goal_status": "active"}],
            "crash_after_turn": 1,
        },
    )
    point_at_fake_appserver(mod, monkeypatch, script)
    monkeypatch.setattr(mod, "_run_task_exec_fallback", lambda task: None)

    task = insert_and_claim_task(mod)
    mod.run_task(task)

    row = fetch_task(mod, task["id"])
    assert row["status"] == "failed"
    assert row["status"] != "running"
    assert row["error"]


def test_appserver_startup_failure_uses_exec_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A spawn-time app-server failure routes to the existing codex exec fallback."""
    mod = load_builder(tmp_path, monkeypatch)
    mod.ensure_table()
    monkeypatch.setattr(mod, "_appserver_cmd", lambda: ["/nonexistent/codex-binary-xyz"])
    fallback_calls: list[int] = []

    def record_exec_fallback(task: dict) -> bool:
        """Record the task id passed to the exec fallback without launching Codex."""
        fallback_calls.append(task["id"])
        return True

    monkeypatch.setattr(mod, "_run_task_exec_fallback", record_exec_fallback)

    task = insert_and_claim_task(mod)
    mod.run_task(task)

    assert fallback_calls == [task["id"]]


def test_gate_verify_nonzero_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-zero independent VERIFY command fails the acceptance gate."""
    mod = load_builder(tmp_path, monkeypatch)
    mod.ensure_table()
    workdir = tmp_path / "wd"
    workdir.mkdir()
    (workdir / "RESULT.txt").write_text('VERIFY: python3 -c "import sys; sys.exit(3)"\n')

    class C:
        """Minimal fake client whose advisory review reports no findings."""

        def request(self, method: str, params: dict, timeout: int | None = None) -> dict:
            return {"turn": {}, "reviewThreadId": "rv-clean", "findings": []}

    ok, reason = mod._run_acceptance_gate(C(), "t1", workdir, lambda *_: None)

    assert ok is False
    assert "VERIFY" in reason


def test_gate_all_pass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A clean review and zero-exit independent VERIFY command pass the gate."""
    mod = load_builder(tmp_path, monkeypatch)
    mod.ensure_table()
    workdir = tmp_path / "wd"
    workdir.mkdir()
    (workdir / "RESULT.txt").write_text('VERIFY: python3 -c "print(1)"\n')

    class C:
        """Minimal fake client whose advisory review reports no findings."""

        def request(self, method: str, params: dict, timeout: int | None = None) -> dict:
            return {"turn": {}, "reviewThreadId": "rv-clean", "findings": []}

    ok, reason = mod._run_acceptance_gate(C(), "t1", workdir, lambda *_: None)

    assert ok is True
    assert reason == ""


def test_gate_review_blocker_findings_are_advisory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review blocker findings are logged but do not fail a passing VERIFY gate."""
    mod = load_builder(tmp_path, monkeypatch)
    mod.ensure_table()
    workdir = tmp_path / "wd"
    workdir.mkdir()
    (workdir / "RESULT.txt").write_text('VERIFY: python3 -c "print(1)"\n')
    logs: list[str] = []

    class C:
        """Fake client that returns legacy findings from the advisory review call."""

        def request(self, method: str, params: dict, timeout: int | None = None) -> dict:
            if method == "review/start":
                return {
                    "turn": {},
                    "reviewThreadId": "rv-blocker",
                    "findings": [{"severity": "blocker", "title": "broken ingestion"}],
                }
            return {}

    ok, reason = mod._run_acceptance_gate(C(), "t1", workdir, logs.append)

    assert ok is True
    assert reason == ""
    assert any(line.strip() == "advisory review started: reviewThreadId=rv-blocker" for line in logs)
