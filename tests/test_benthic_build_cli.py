from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent


def _read_route_row(db_path: Path) -> tuple[int, int | None, int]:
    """Read the routing columns inserted by the benthic-build CLI."""
    with sqlite3.connect(str(db_path)) as conn:
        row = conn.execute(
            "SELECT chat_id, message_id, requested_by FROM build_tasks ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row is not None
    return row


def test_build_env_routing_overrides_flags(tmp_path: Path) -> None:
    """BENTHIC_BUILD_* env vars are authoritative over conflicting CLI flags."""
    db_path = tmp_path / "agent.db"
    result = subprocess.run(
        [
            sys.executable,
            str(REPO / "bin" / "benthic-build"),
            "start",
            "thing",
            "--chat",
            "-1001234567890",
            "--message",
            "1",
            "--user",
            "2",
        ],
        input="Build a thing that does something useful.",
        text=True,
        capture_output=True,
        env={
            **os.environ,
            "BENTHIC_DB": str(db_path),
            "BENTHIC_BUILD_CHAT": "-999",
            "BENTHIC_BUILD_MESSAGE": "42",
            "BENTHIC_BUILD_USER": "7",
        },
    )

    assert result.returncode == 0, result.stderr
    assert _read_route_row(db_path) == (-999, 42, 7)


def test_build_flags_used_when_env_absent(tmp_path: Path) -> None:
    """Manual CLI flags remain the fallback when no routing env is present."""
    db_path = tmp_path / "agent.db"
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("BENTHIC_BUILD_")
    }
    env["BENTHIC_DB"] = str(db_path)

    result = subprocess.run(
        [
            sys.executable,
            str(REPO / "bin" / "benthic-build"),
            "start",
            "thing",
            "--chat",
            "-555",
            "--message",
            "9",
            "--user",
            "3",
        ],
        input="Build a thing that does something useful.",
        text=True,
        capture_output=True,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert _read_route_row(db_path) == (-555, 9, 3)


def test_env_route_overrides_file_and_flags(tmp_path: Path) -> None:
    """Per-invocation env routing wins over BOTH the shared route file and CLI
    flags. Precedence was flipped to ENV > FILE > flag so two concurrent build
    starts can't clobber each other's routing via the shared file — PR #1 finding."""
    db_path = tmp_path / "agent.db"
    (tmp_path / ".build-route.json").write_text(json.dumps({
        "chat_id": -777,
        "message_id": 55,
        "user_id": 9,
        "written_at": time.time(),
    }))

    result = subprocess.run(
        [
            sys.executable,
            str(REPO / "bin" / "benthic-build"),
            "start",
            "thing",
            "--chat",
            "-111",
            "--message",
            "1",
            "--user",
            "2",
        ],
        input="Build a thing that does something useful.",
        text=True,
        capture_output=True,
        env={
            **os.environ,
            "BENTHIC_DB": str(db_path),
            "BENTHIC_BUILD_CHAT": "-999",
            "BENTHIC_BUILD_MESSAGE": "42",
            "BENTHIC_BUILD_USER": "7",
        },
    )

    assert result.returncode == 0, result.stderr
    assert _read_route_row(db_path) == (-999, 42, 7)


def test_route_file_used_when_env_absent(tmp_path: Path) -> None:
    """With no env routing, the shared route file still wins over CLI flags
    (ENV > FILE > flag), so the file remains a working fallback."""
    db_path = tmp_path / "agent.db"
    (tmp_path / ".build-route.json").write_text(json.dumps({
        "chat_id": -777,
        "message_id": 55,
        "user_id": 9,
        "written_at": time.time(),
    }))
    env = {k: v for k, v in os.environ.items() if not k.startswith("BENTHIC_BUILD_")}
    env["BENTHIC_DB"] = str(db_path)

    result = subprocess.run(
        [
            sys.executable,
            str(REPO / "bin" / "benthic-build"),
            "start",
            "thing",
            "--chat",
            "-111",
            "--message",
            "1",
            "--user",
            "2",
        ],
        input="Build a thing that does something useful.",
        text=True,
        capture_output=True,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert _read_route_row(db_path) == (-777, 55, 9)


def test_stale_route_file_ignored(tmp_path: Path) -> None:
    """A stale route file is ignored so env routing remains the fallback."""
    db_path = tmp_path / "agent.db"
    (tmp_path / ".build-route.json").write_text(json.dumps({
        "chat_id": -777,
        "message_id": 55,
        "user_id": 9,
        "written_at": time.time() - 100000,
    }))

    result = subprocess.run(
        [
            sys.executable,
            str(REPO / "bin" / "benthic-build"),
            "start",
            "thing",
        ],
        input="Build a thing that does something useful.",
        text=True,
        capture_output=True,
        env={
            **os.environ,
            "BENTHIC_DB": str(db_path),
            "BENTHIC_BUILD_CHAT": "-999",
            "BENTHIC_BUILD_MESSAGE": "42",
            "BENTHIC_BUILD_USER": "7",
        },
    )

    assert result.returncode == 0, result.stderr
    assert _read_route_row(db_path) == (-999, 42, 7)
