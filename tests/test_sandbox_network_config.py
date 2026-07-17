"""Regression coverage for the sandbox's credential-free RPC configuration."""

import json
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
CHAINS_PATH = ROOT / "sandbox" / "chains.json"
ALLOWED_HOSTS_PATH = ROOT / "sandbox" / "allowed-hosts.txt"


def _chains() -> dict:
    """Load the chain registry exactly as the sandbox image consumes it."""
    return json.loads(CHAINS_PATH.read_text(encoding="utf-8"))


def _allowed_hosts() -> set[str]:
    """Return active host entries while ignoring comments and blank lines."""
    return {
        line.strip()
        for line in ALLOWED_HOSTS_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }


def test_ethereum_prefers_working_credential_free_rpc_endpoints():
    """Ethereum reads must not begin with endpoints requiring an API key."""
    rpcs = _chains()["ethereum"]["rpcs"]

    assert rpcs[:2] == [
        "https://ethereum-rpc.publicnode.com",
        "https://public.1rpc.io/eth",
    ]
    assert "https://rpc.ankr.com/eth" not in rpcs
    assert "https://eth-mainnet.g.alchemy.com/v2/demo" not in rpcs


def test_every_configured_rpc_hostname_is_allowlisted():
    """Static DNS and egress rules must cover every configured chain RPC."""
    configured_hosts = {
        urlparse(rpc).hostname
        for chain in _chains().values()
        for rpc in chain["rpcs"]
    }

    assert None not in configured_hosts
    assert configured_hosts <= _allowed_hosts()
