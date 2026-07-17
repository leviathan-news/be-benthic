from __future__ import annotations

import importlib.util
import shutil
import types
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest


BUILDER_PATH = Path(__file__).parent.parent / "benthic-builder.py"


class FakePopen:
    """Minimal subprocess stand-in used so tests never invoke the real Codex CLI."""

    pid = 43210

    def __init__(self, *args: object, **kwargs: object) -> None:
        """Accept the same broad construction surface as subprocess.Popen."""

    def communicate(self, input: str | None = None, timeout: int | None = None) -> None:
        """Return without writing RESULT.txt so run_task follows its normal failure path."""


def load_builder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """Load the hyphenated builder module from disk with all paths pointed at tmp_path."""
    home = tmp_path / "home"
    base = tmp_path / "base"
    build_root = tmp_path / "builds"
    log_root = tmp_path / "logs"
    db_file = tmp_path / "agent.db"
    home.mkdir()

    # The module reads these environment variables and creates BUILD_ROOT/LOG_ROOT at import.
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("BENTHIC_BASE", str(base))
    monkeypatch.setenv("BUILD_ROOT", str(build_root))
    monkeypatch.setenv("BUILD_LOG_ROOT", str(log_root))
    monkeypatch.setenv("BENTHIC_DB", str(db_file))

    spec = importlib.util.spec_from_file_location(
        f"benthic_builder_test_{uuid.uuid4().hex}",
        BUILDER_PATH,
    )
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # Keep the live module globals tied to pytest-owned paths even if tests mutate them later.
    mod.BUILD_ROOT = build_root
    mod.LOG_ROOT = log_root
    mod.DB_FILE = db_file
    mod.TASK_TIMEOUT = 5

    # Network and real Codex execution are both forbidden in these regression tests.
    monkeypatch.setattr(mod, "tg_send", lambda *args, **kwargs: None)
    monkeypatch.setattr(mod.subprocess, "Popen", FakePopen)
    return mod


def insert_pending_task(mod: types.ModuleType) -> None:
    """Insert the minimal pending queue row required for claim_next_task()."""
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
                "please build a test project",
                "Build a minimal test project.",
                "test-project",
                None,
                "pending",
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()


def fetch_task(mod: types.ModuleType, task_id: int) -> dict:
    """Return a fresh copy of a build_tasks row after run_task mutates the database."""
    with mod.db() as conn:
        row = conn.execute(
            "SELECT status, error, log_path, pid FROM build_tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
    assert row is not None
    return dict(row)


def test_run_task_survives_reaped_log_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mod = load_builder(tmp_path, monkeypatch)
    mod.LOG_ROOT = tmp_path / "reaped-log-root"
    mod.LOG_ROOT.mkdir(parents=True)
    mod.ensure_table()
    insert_pending_task(mod)
    task = mod.claim_next_task()
    assert task is not None

    # Simulate the OS /tmp reaper deleting the empty log directory after task claim.
    shutil.rmtree(mod.LOG_ROOT)
    try:
        mod.run_task(task)
    except Exception as exc:  # pragma: no cover - this is the regression assertion.
        pytest.fail(f"run_task raised unexpectedly: {exc}")

    row = fetch_task(mod, task["id"])
    assert row["status"] == "failed"
    assert row["status"] != "running"
    assert row["error"]
    assert mod.LOG_ROOT.exists()


def test_run_task_creates_missing_log_parent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mod = load_builder(tmp_path, monkeypatch)
    mod.ensure_table()
    insert_pending_task(mod)
    task = mod.claim_next_task()
    assert task is not None

    # Removing the parent verifies run_task recreates it immediately before opening the log.
    shutil.rmtree(Path(task["log_path"]).parent)
    mod.run_task(task)

    assert Path(task["log_path"]).parent.exists()


def test_run_task_no_zombie_on_unexpected_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = load_builder(tmp_path, monkeypatch)
    mod.ensure_table()
    insert_pending_task(mod)
    task = mod.claim_next_task()
    assert task is not None

    def broken_parse_result(workdir: Path) -> tuple[str | None, str | None]:
        """Raise after subprocess completion to prove outer failure handling records a terminal state."""
        raise RuntimeError("parse_result exploded")

    monkeypatch.setattr(mod, "parse_result", broken_parse_result)
    try:
        mod.run_task(task)
    except Exception as exc:  # pragma: no cover - this is the zombie prevention assertion.
        pytest.fail(f"run_task raised unexpectedly: {exc}")

    row = fetch_task(mod, task["id"])
    assert row["status"] == "failed"
    assert row["status"] != "running"
    assert row["error"]
    assert "parse_result exploded" in row["error"]
