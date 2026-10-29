"""Tests for the codex usage monitor's auth-token expiry warning."""

import base64
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).parent.parent))

import codex_usage_report as report


def _jwt_with_exp(exp_epoch: int) -> str:
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": exp_epoch}).encode()
    ).rstrip(b"=").decode()
    return f"eyJhbGciOiJub25lIn0.{payload}.sig"


def _write_auth(tmp_path, exp_epoch) -> Path:
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps(
        {"tokens": {"access_token": _jwt_with_exp(exp_epoch)}}
    ))
    return auth


NOW = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)


def test_no_warning_when_token_fresh(tmp_path):
    auth = _write_auth(tmp_path, int((NOW + timedelta(days=6)).timestamp()))
    assert report._auth_token_expiry_line(auth, NOW) is None


def test_warns_when_token_expires_within_five_days(tmp_path):
    auth = _write_auth(tmp_path, int((NOW + timedelta(days=2)).timestamp()))
    line = report._auth_token_expiry_line(auth, NOW)
    assert line is not None
    assert "2d" in line
    assert "⚠️" in line


def test_expired_token_marked_expired(tmp_path):
    auth = _write_auth(tmp_path, int((NOW - timedelta(hours=3)).timestamp()))
    line = report._auth_token_expiry_line(auth, NOW)
    assert line is not None
    assert "EXPIRED" in line


def test_unreadable_or_malformed_auth_returns_none(tmp_path):
    missing = tmp_path / "nope.json"
    assert report._auth_token_expiry_line(missing, NOW) is None
    garbage = tmp_path / "auth.json"
    garbage.write_text("{not json")
    assert report._auth_token_expiry_line(garbage, NOW) is None
    nonjwt = tmp_path / "auth2.json"
    nonjwt.write_text(json.dumps({"tokens": {"access_token": "opaque"}}))
    assert report._auth_token_expiry_line(nonjwt, NOW) is None


def test_dry_run_appends_warning_to_report(tmp_path, capsys, monkeypatch):
    auth = _write_auth(
        tmp_path,
        int((datetime.now(timezone.utc) + timedelta(days=1)).timestamp()),
    )
    monkeypatch.setattr(report, "AUTH_PATH", auth)
    rl = {"rateLimits": {"planType": "prolite", "primary": {
        "usedPercent": 6, "windowDurationMins": 10080}}}
    with patch.object(report, "_rpc_query",
                      return_value={"account/rateLimits/read": rl}):
        with patch.object(sys, "argv", ["codex_usage_report.py", "--dry-run"]):
            assert report.main() == 0
    out = capsys.readouterr().out
    assert "weekly: 6%" in out
    assert "⚠️" in out and "auth token" in out


def test_warning_still_reported_when_usage_query_fails(tmp_path, capsys,
                                                       monkeypatch):
    """The token-expiry warning must survive exactly the failure mode it
    predicts: an expired token kills the app-server query, and the monitor
    must still deliver the warning instead of dying silently."""
    auth = _write_auth(
        tmp_path,
        int((datetime.now(timezone.utc) - timedelta(hours=1)).timestamp()),
    )
    monkeypatch.setattr(report, "AUTH_PATH", auth)
    with patch.object(report, "_rpc_query", return_value={}):
        with patch.object(sys, "argv", ["codex_usage_report.py", "--dry-run"]):
            assert report.main() == 0
    out = capsys.readouterr().out
    assert "EXPIRED" in out
    assert "usage query failed" in out
