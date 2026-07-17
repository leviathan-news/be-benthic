import json, os, sys, importlib.util
from pathlib import Path
import pytest

REPO = Path(__file__).resolve().parent.parent
FAKE = REPO / "tests" / "fake_appserver.py"

def _load():
    spec = importlib.util.spec_from_file_location("appserver_client", REPO / "appserver_client.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m

def _client(mod, tmp_path, script):
    p = tmp_path / "script.json"; p.write_text(json.dumps(script))
    env = dict(os.environ, FAKE_APPSERVER_SCRIPT=str(p))
    return mod.AppServerClient(cmd=[sys.executable, str(FAKE)], cwd=str(tmp_path), env=env)

def test_initialize_and_thread_start(tmp_path):
    mod = _load()
    c = _client(mod, tmp_path, {"thread_id": "t1", "turns": []})
    c.start()
    try:
        assert c.request("thread/start", {}, timeout=10)["threadId"] == "t1"
    finally:
        c.close()

def test_request_timeout_raises(tmp_path):
    mod = _load()
    c = _client(mod, tmp_path, {"thread_id": "t1", "turns": [], "sleep_methods": {"thread/goal/get": 2.0}})
    c.start()
    try:
        with pytest.raises(mod.AppServerError):
            c.request("thread/goal/get", {"threadId": "t1"}, timeout=0.4)
    finally:
        c.close()

def test_notifications_dispatched(tmp_path):
    mod = _load()
    got = []
    c = _client(mod, tmp_path, {"thread_id": "t1", "turns": [{"goal_status": "complete"}]})
    c.on_notification = got.append
    c.start()
    try:
        c.request("thread/start", {}, timeout=10)
        c.request("turn/start", {"threadId": "t1", "input": "go"}, timeout=10)
    finally:
        c.close()
    methods = [n.get("method") for n in got]
    assert "thread/goal/updated" in methods and "turn/completed" in methods

def test_connection_loss_rejects_pending(tmp_path):
    mod = _load()
    c = _client(mod, tmp_path, {"thread_id": "t1", "turns": [{"goal_status": "active"}], "crash_after_turn": 1})
    c.start()
    try:
        c.request("thread/start", {}, timeout=10)
        try:
            c.request("turn/start", {"threadId": "t1", "input": "go"}, timeout=10)
        except mod.AppServerError:
            pass
        with pytest.raises(mod.AppServerError):
            c.request("thread/goal/get", {"threadId": "t1"}, timeout=5)
    finally:
        c.close()
