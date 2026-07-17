#!/bin/bash
# Trusted host wrapper for Benthic's runtime-mediated sandbox execution.
# Model shells never call this script or receive Docker access directly.
# All container security invariants are enforced here for the validated code.
#
# Usage: run-sandbox.sh '<python code>'

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
OUTPUT_FILTER="$SCRIPT_DIR/bounded_output.py"
OUTPUT_LIMIT_BYTES=8192

if [ -z "${1:-}" ]; then
    echo "ERROR: No code provided. Usage: run-sandbox.sh '<python code>'"
    exit 1
fi

# DNS-exfil defense: resolve allowlisted hosts via static /etc/hosts entries
# (--add-host, from .resolved-hosts written by setup-network.sh) and point DNS at a
# black-hole (192.0.2.1, RFC5737 TEST-NET-1, unroutable) so any NON-allowlisted name
# fails to resolve. glibc consults /etc/hosts before DNS, so allowlisted hosts keep
# working while a <secret>.attacker.com lookup dead-ends — PR #1 finding.
ADD_HOSTS=""
RESOLVED_HOSTS="$(dirname "$0")/.resolved-hosts"
if [ -f "$RESOLVED_HOSTS" ]; then
    while IFS=' ' read -r _host _ip; do
        [ -n "$_host" ] && [ -n "$_ip" ] && ADD_HOSTS="$ADD_HOSTS --add-host=$_host:$_ip"
    done < "$RESOLVED_HOSTS"
fi

# Merge Docker stdout/stderr into the trusted filter before the host caller can
# capture it. The filter keeps at most OUTPUT_LIMIT_BYTES while continuing to
# drain the pipe through EOF, so verbose sandbox code cannot block Docker.
set +e
timeout 120 docker run --rm --read-only \
    --tmpfs /tmp:size=100M \
    --memory=512m --cpus=1 \
    --pids-limit=64 \
    --security-opt=no-new-privileges:true \
    --network=benthic-sandbox-net \
    --dns=192.0.2.1 \
    $ADD_HOSTS \
    --user sandbox \
    benthic-sandbox python3 -c "$1" 2>&1 \
    | python3 "$OUTPUT_FILTER" "$OUTPUT_LIMIT_BYTES"
PIPE_STATUSES=("${PIPESTATUS[@]}")
set -e

SANDBOX_STATUS="${PIPE_STATUSES[0]}"
FILTER_STATUS="${PIPE_STATUSES[1]}"
if [ "$FILTER_STATUS" -ne 0 ]; then
    echo "ERROR: sandbox output filter failed (exit $FILTER_STATUS)." >&2
    # Preserve an existing Docker/timeout failure, especially timeout status
    # 124. Use a distinct wrapper failure only when execution itself succeeded.
    if [ "$SANDBOX_STATUS" -eq 0 ]; then
        exit 125
    fi
fi
exit "$SANDBOX_STATUS"
