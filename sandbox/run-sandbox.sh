#!/bin/bash
# Wrapper for benthic sandbox execution.
# Claude CLI calls this via Bash tool — never docker run directly.
# All security invariants enforced here regardless of what Claude passes.
#
# Usage: run-sandbox.sh '<python code>'

set -euo pipefail

if [ -z "${1:-}" ]; then
    echo "ERROR: No code provided. Usage: run-sandbox.sh '<python code>'"
    exit 1
fi

# Pass only safe env vars to the container — API keys for data access, never wallet/bot secrets
SANDBOX_ENV=""
[ -n "${ETHERSCAN_API_KEY:-}" ] && SANDBOX_ENV="$SANDBOX_ENV -e ETHERSCAN_API_KEY=$ETHERSCAN_API_KEY"

exec timeout 120 docker run --rm --read-only \
    --tmpfs /tmp:size=100M \
    --memory=512m --cpus=1 \
    --pids-limit=64 \
    --security-opt=no-new-privileges:true \
    --network=benthic-sandbox-net \
    --user sandbox \
    $SANDBOX_ENV \
    benthic-sandbox python3 -c "$1" 2>&1
