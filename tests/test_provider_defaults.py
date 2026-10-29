"""Retired providers must not be contacted when the default Codex call fails."""

import importlib.util
import logging
import logging.handlers
from pathlib import Path
import sys

import pytest

from providers import ClaudeProvider, CodexProvider


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("filename", ["benthic-bot.py", "ln-agent.py"])
@pytest.mark.parametrize("order", [None, "", "  "])
def test_default_provider_failure_never_contacts_retired_claude(
        monkeypatch, tmp_path, filename, order):
    if order is None:
        monkeypatch.delenv("PROVIDER_ORDER", raising=False)
    else:
        monkeypatch.setenv("PROVIDER_ORDER", order)
    monkeypatch.setenv("BOT_TOKEN", "test:inert")
    monkeypatch.setenv("WALLET_PRIVATE_KEY", "")
    monkeypatch.setenv("WALLET_KEY_FILE", str(tmp_path / "missing-wallet"))
    monkeypatch.setenv("BENTHIC_DB", str(tmp_path / "agent.db"))
    monkeypatch.setenv("BENTHIC_LOG_FILE", str(tmp_path / "benthic.log"))
    monkeypatch.setattr(logging, "FileHandler", lambda *a, **k: logging.NullHandler())
    monkeypatch.setattr(logging.handlers, "RotatingFileHandler", lambda *a, **k: logging.NullHandler())
    calls = []
    monkeypatch.setattr(CodexProvider, "is_available", lambda self: True)
    monkeypatch.setattr(ClaudeProvider, "is_available", lambda self: True)
    monkeypatch.setattr(CodexProvider, "ask", lambda *a, **k: calls.append("codex") or "")
    monkeypatch.setattr(ClaudeProvider, "ask", lambda *a, **k: calls.append("claude") or "retired fallback")
    name = "provider_default_" + Path(filename).stem.replace("-", "_")
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
        answer = module._provider_chain.ask("Synthetic failure probe", tools="__none__")
        assert answer == ""
        assert calls == ["codex"]
        assert module._provider_chain.names() == ["codex"]
        if filename == "benthic-bot.py":
            assert '"provider":"claude"' not in module._model_routing_runtime_item().text
    finally:
        if hasattr(module, "_PROC_POOL"):
            module._PROC_POOL.shutdown(wait=True, cancel_futures=True)
        sys.modules.pop(name, None)
