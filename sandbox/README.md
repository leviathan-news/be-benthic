# Benthic Sandbox

Isolated Docker environment for Benthic Bot code execution.

Normal chat use is runtime-mediated: the first provider pass emits a validated
`[SANDBOX]` block and trusted bot Python invokes `run-sandbox.sh`. Model shells
never receive Docker access. The successful result is interpreted in one
`tools="__none__"` provider pass. For Codex 0.144, that is a hard text-only
invocation using `--ignore-user-config`, `--ignore-rules`, a fresh empty cwd,
read-only sandboxing, `approval_policy=never`, `web_search="disabled"`, and
explicit disables for shell, apps/plugins, browser/computer/image tools,
multi-agent, hooks, memories, remote plugins, and tool suggestions.

## Setup

```bash
# 1. Build the image
docker build -t benthic-sandbox sandbox/

# 2. Set up the network with allowlisted hosts
sudo sandbox/setup-network.sh

# 3. Test it
sandbox/run-sandbox.sh "print('hello from sandbox')"
sandbox/run-sandbox.sh "from web3 import Web3; print(Web3.is_address('0x0000000000000000000000000000000000000000'))"
```

## What's inside

Python 3.12 with: web3, requests, pandas, matplotlib, eth-abi

## What's NOT inside

- No wallet key, bot token, or Telegram session
- No reusable API credential enters the container
- No curl, wget, or git
- No access to agent.db or host filesystem

## Output transport

`run-sandbox.sh` merges Docker stdout/stderr and pipes the stream through
`bounded_output.py` before host capture. The filter drains stdin to EOF while
retaining at most 8192 bytes, including a deterministic `[output truncated]`
marker. It does not log or persist output. The wrapper preserves the Docker or
inner-timeout status (including 124) and reports a filter failure explicitly.

## Network

Outbound restricted to RPCs, block explorers, and data APIs only.
Ethereum reads use the credential-free PublicNode and 1RPC endpoints. Their
hostnames must remain in `allowed-hosts.txt`, and `setup-network.sh` must be
re-run after any endpoint change so static DNS and egress rules stay aligned.
Edit `allowed-hosts.txt` and re-run `sudo setup-network.sh` to add endpoints.

## Resource limits

- 512MB RAM, 1 CPU, 120s timeout, 100MB tmpfs scratch
- Read-only filesystem, non-root user, no-new-privileges
- PID limit: 64 (fork bomb protection)
- 8192-byte merged stdout/stderr cap before host capture

## Deployment

Deploy `run-sandbox.sh` and `bounded_output.py` together. The filter runs on the
trusted host and is not baked into the Docker image.

## Troubleshooting

If sandbox API calls fail, allowlisted IPs may have rotated (CDN/failover).
Re-run `sudo setup-network.sh` to refresh DNS resolutions.
