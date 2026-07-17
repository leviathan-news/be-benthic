#!/bin/bash
# Setup Docker network with iptables allowlist for benthic sandbox containers.
# Run once on the LXC host (or re-run after editing allowed-hosts.txt).
# Requires: docker, iptables, root/sudo.

set -euo pipefail

NETWORK_NAME="benthic-sandbox-net"
CHAIN_NAME="BENTHIC-SANDBOX"
HOSTS_FILE="$(dirname "$0")/allowed-hosts.txt"

if [ ! -f "$HOSTS_FILE" ]; then
    echo "ERROR: $HOSTS_FILE not found"
    exit 1
fi

# Create Docker network if it doesn't exist
if ! docker network inspect "$NETWORK_NAME" &>/dev/null; then
    echo "Creating Docker network: $NETWORK_NAME"
    docker network create "$NETWORK_NAME"
else
    echo "Docker network $NETWORK_NAME already exists"
fi

# Get the bridge interface name for the network.
# Docker auto-assigns br-<short_id> when no explicit bridge name is set.
# The .Options template returns "<no value>" (not empty) when unset.
BRIDGE=$(docker network inspect "$NETWORK_NAME" -f '{{.Options.com.docker.network.bridge.name}}' 2>/dev/null)
if [ -z "$BRIDGE" ] || [ "$BRIDGE" = "<no value>" ]; then
    NET_ID=$(docker network inspect "$NETWORK_NAME" -f '{{.Id}}' | head -c 12)
    BRIDGE="br-${NET_ID}"
fi

echo "Bridge interface: $BRIDGE"

# Flush existing chain if it exists, or create it
if iptables -L "$CHAIN_NAME" -n &>/dev/null; then
    echo "Flushing existing $CHAIN_NAME chain"
    iptables -F "$CHAIN_NAME"
else
    echo "Creating $CHAIN_NAME chain"
    iptables -N "$CHAIN_NAME"
fi

# Remove existing jump rule if present, then re-add
iptables -D FORWARD -i "$BRIDGE" -j "$CHAIN_NAME" 2>/dev/null || true
iptables -I FORWARD -i "$BRIDGE" -j "$CHAIN_NAME"

# DNS egress is intentionally NOT opened. Sandbox containers resolve the allowlisted
# hosts via static /etc/hosts entries (run-sandbox.sh injects --add-host from the
# .resolved-hosts file written below) and point at a black-hole resolver for
# everything else, so model-supplied code cannot exfiltrate data via DNS lookups
# like <secret>.attacker.com. Any direct external port-53 attempt falls through to
# the default DROP at the end of this chain — PR #1 finding.

# Allow established/related connections (return traffic)
iptables -A "$CHAIN_NAME" -m state --state ESTABLISHED,RELATED -j ACCEPT

# Resolve each allowed host and add iptables rules. Also record host->IP pairs to
# .resolved-hosts so run-sandbox.sh can inject them as static /etc/hosts entries
# (--add-host): the container reaches allowlisted hosts WITHOUT DNS, letting us
# black-hole all other DNS and close the exfil channel.
RESOLVED_HOSTS_FILE="$(dirname "$0")/.resolved-hosts"
: > "$RESOLVED_HOSTS_FILE"
while IFS= read -r line; do
    # Skip comments and blank lines
    line=$(echo "$line" | sed 's/#.*//' | xargs)
    [ -z "$line" ] && continue

    echo "Resolving: $line"
    # Resolve hostname to IPs (may return multiple)
    ips=$(dig +short "$line" A 2>/dev/null | grep -E '^[0-9]+\.' || true)
    if [ -z "$ips" ]; then
        echo "  WARNING: could not resolve $line — skipping"
        continue
    fi
    for ip in $ips; do
        echo "  Allowing $ip (443/tcp)"
        iptables -A "$CHAIN_NAME" -p tcp -d "$ip" --dport 443 -j ACCEPT
        echo "$line $ip" >> "$RESOLVED_HOSTS_FILE"
    done
done < "$HOSTS_FILE"

# Default: drop all other outbound from sandbox containers
iptables -A "$CHAIN_NAME" -j DROP

echo ""
echo "Sandbox network setup complete."
echo "  Network: $NETWORK_NAME"
echo "  Chain: $CHAIN_NAME"
echo "  Allowed hosts: $(grep -cv '^\s*#\|^\s*$' "$HOSTS_FILE")"
echo "  Default policy: DROP"
