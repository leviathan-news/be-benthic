"""Suite-wide safety rails and white-label env defaults.

RAM ceiling: a runaway red-phase test (supervisor retry loop with a zero-delay
monkeypatched sleep whose fake listener raised before its stop guard) once
allocated >50GB and crashed a workstation. A hard address-space cap turns any
future runaway into a MemoryError test failure instead of a dead machine.
4GB is ~50x the suite's honest peak.
"""

import importlib.util
import os
from pathlib import Path
import resource
import sys
import tempfile

import pytest

_RAM_CAP_BYTES = 4 * 1024 ** 3  # 4 GiB
_BOT_STATE_DIR = tempfile.TemporaryDirectory(prefix="lev-news-pytest-")
_BOT_STATE_ROOT = Path(_BOT_STATE_DIR.name)

# Bot modules initialize SQLite and file logging at import. Redirect both before
# pytest imports any test module so offline tests cannot touch repository state.
os.environ["BENTHIC_DB"] = str(_BOT_STATE_ROOT / "agent.db")
os.environ["BENTHIC_LOG_FILE"] = str(_BOT_STATE_ROOT / "benthic.log")

# White-label build: these are required at import time by ln-agent.py and
# benthic-bot.py (no baked-in defaults). Provide harmless test values.
os.environ.setdefault("BOT_HQ_GROUP_ID", "-1001234567890")
os.environ.setdefault("CHANNELS", '["@test_channel"]')
os.environ.setdefault("AGENTS_GROUP_ID", "-1001234567891")
os.environ.setdefault("BOT_TOKEN", "000000:TEST_TOKEN_FOR_UNIT_TESTS")
# Prevent wallet key file read from touching a real key during tests
os.environ.setdefault("WALLET_KEY_FILE", "/dev/null")
os.environ.setdefault("WALLET_PRIVATE_KEY", "")
# The shipped prompts and test fixtures use the Benthic persona by default.
os.environ.setdefault("AGENT_NAME", "Benthic")
os.environ.setdefault("BOT_USERNAME", "benthic_bot")
os.environ.setdefault("OPERATOR_IDS", "[111000111]")
# The API tests exercise endpoints without configuring a bearer token; the
# explicit fail-closed behavior has its own dedicated tests.
os.environ.setdefault("API_ALLOW_UNAUTHENTICATED", "1")


def _apply_ram_cap() -> None:
    # RLIMIT_DATA caps malloc'd memory on macOS (Ventura+) and Linux.
    # Best-effort: never let the guard itself break the suite.
    for limit_name in ("RLIMIT_DATA", "RLIMIT_AS"):
        limit = getattr(resource, limit_name, None)
        if limit is None:
            continue
        try:
            soft, hard = resource.getrlimit(limit)
            new_hard = hard if hard != resource.RLIM_INFINITY else _RAM_CAP_BYTES
            resource.setrlimit(limit, (min(_RAM_CAP_BYTES, new_hard), new_hard))
            return  # first one that sticks is enough
        except (ValueError, OSError):
            continue


if sys.platform != "win32":
    _apply_ram_cap()

# Resolve path to ln-agent.py relative to this conftest
AGENT_PATH = Path(__file__).parent.parent / "ln-agent.py"


@pytest.fixture(scope="session")
def agent():
    """Import ln-agent.py as a module named 'agent' for the test session."""
    spec = importlib.util.spec_from_file_location("agent", AGENT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def tmp_db(agent, tmp_path):
    """Create a temporary AgentDB instance with a fresh SQLite database."""
    db_path = tmp_path / "test_agent.db"
    db = agent.AgentDB(db_path)
    yield db
    db.close()
