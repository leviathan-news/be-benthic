"""Tests for ln-agent cycle timeout behavior."""

import asyncio
import importlib.util
import sys
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


def _load_agent_module():
    """Import ln-agent.py under a Python-safe module name for direct tests."""
    spec = importlib.util.spec_from_file_location(
        "ln_agent_import", ROOT / "ln-agent.py")
    mod = importlib.util.module_from_spec(spec)
    # Don't actually run the agent — just expose the helpers.
    # ln-agent.py defines run_loop() but only calls it from __main__.
    sys.modules["ln_agent_import"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def agent_module():
    """Restore patched module attributes so tests do not leak state."""
    mod = _load_agent_module()
    missing = object()
    original_run_agent = mod.run_agent
    original_cycle_deadline = getattr(mod, "CYCLE_DEADLINE", missing)
    try:
        yield mod
    finally:
        mod.run_agent = original_run_agent
        if original_cycle_deadline is missing:
            delattr(mod, "CYCLE_DEADLINE")
        else:
            mod.CYCLE_DEADLINE = original_cycle_deadline


def test_guarded_cycle_aborts_hung_run_agent_fast(agent_module):
    async def _hang():
        """Simulate a blocked await inside the agent cycle."""
        await asyncio.sleep(9999)

    agent_module.run_agent = _hang
    agent_module.CYCLE_DEADLINE = 0.2

    started = time.monotonic()
    assert hasattr(agent_module, "_run_guarded_cycle")
    asyncio.run(agent_module._run_guarded_cycle())
    elapsed = time.monotonic() - started

    assert elapsed < 2


def test_guarded_cycle_allows_normal_run_agent(agent_module, caplog):
    async def _noop():
        """Return immediately to model a healthy agent cycle."""
        return None

    agent_module.run_agent = _noop
    agent_module.CYCLE_DEADLINE = 0.2

    started = time.monotonic()
    assert hasattr(agent_module, "_run_guarded_cycle")
    asyncio.run(agent_module._run_guarded_cycle())
    elapsed = time.monotonic() - started

    assert elapsed < 2
    assert "Agent cycle exceeded" not in caplog.text
