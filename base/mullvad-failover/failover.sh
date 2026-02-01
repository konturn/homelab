#!/bin/bash
# Mullvad WireGuard Adaptive Failover Script
# Tests Chicago Mullvad servers and switches to healthiest one if current is unhealthy
#
# Usage: Run via systemd timer every 60 seconds
# Logs: syslog (tag: mullvad-failover)

set -euo pipefail

# Configuration
WIREGUARD_INTERFACE="${WIREGUARD_INTERFACE:-wg-mullvad}"
CONFIG_TEMPLATE="/etc/wireguard/mullvad.conf.template"
CONFIG_PATH="/etc/wireguard/${WIREGUARD_INTERFACE}.conf"
STATE_FILE="/var/lib/mullvad-failover/current-server"
PING_COUNT=3
PING_TIMEOUT=2
MAX_LATENCY_MS=150
MAX_PACKET_LOSS=20

# Chicago Mullvad servers
SERVERS=(
    "us-chi-wg-001.relays.mullvad.net"
    "us-chi-wg-002.relays.mullvad.net"
    "us-chi-wg-003.relays.mullvad.net"
    "us-chi-wg-004.relays.mullvad.net"
)

log() {
    logger -t mullvad-failover "$1"
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1"
}

# Test server health via ping
# Returns: 0 if healthy, 1 if unhealthy
# Outputs: latency in ms (or 9999 if unreachable)
test_server() {
    local server="$1"
    local ip
    
    # Resolve hostname to IP
    ip=$(dig +short "$server" A | head -1)
    if [[ -z "$ip" ]]; then
        echo "9999"
        return 1
    fi
    
    # Ping test
    local ping_result
    if ! ping_result=$(ping -c "$PING_COUNT" -W "$PING_TIMEOUT" "$ip" 2>/dev/null); then
        echo "9999"
        return 1
    fi
    
    # Extract packet loss percentage
    local packet_loss
    packet_loss=$(echo "$ping_result" | grep -oP '\d+(?=% packet loss)' || echo "100")
    
    if [[ "$packet_loss" -gt "$MAX_PACKET_LOSS" ]]; then
        echo "9999"
        return 1
    fi
    
    # Extract average latency
    local latency
    latency=$(echo "$ping_result" | grep -oP 'rtt min/avg/max/mdev = [\d.]+/([\d.]+)' | grep -oP '/[\d.]+' | head -1 | tr -d '/')
    
    if [[ -z "$latency" ]]; then
        echo "9999"
        return 1
    fi
    
    # Convert to integer (floor)
    latency=${latency%.*}
    
    if [[ "$latency" -gt "$MAX_LATENCY_MS" ]]; then
        echo "$latency"
        return 1
    fi
    
    echo "$latency"
    return 0
}

# Get current server from state file
get_current_server() {
    if [[ -f "$STATE_FILE" ]]; then
        cat "$STATE_FILE"
    else
        echo ""
    fi
}

# Save current server to state file
save_current_server() {
    local server="$1"
    mkdir -p "$(dirname "$STATE_FILE")"
    echo "$server" > "$STATE_FILE"
}

# Find the healthiest server
find_best_server() {
    local best_server=""
    local best_latency=9999
    
    for server in "${SERVERS[@]}"; do
        log "Testing $server..."
        local latency
        latency=$(test_server "$server")
        log "  $server: ${latency}ms"
        
        if [[ "$latency" -lt "$best_latency" ]]; then
            best_latency="$latency"
            best_server="$server"
        fi
    done
    
    if [[ -z "$best_server" ]]; then
        log "ERROR: No healthy servers found!"
        return 1
    fi
    
    log "Best server: $best_server (${best_latency}ms)"
    echo "$best_server"
}

# Apply new server configuration
apply_config() {
    local server="$1"
    
    if [[ ! -f "$CONFIG_TEMPLATE" ]]; then
        log "ERROR: Config template not found at $CONFIG_TEMPLATE"
        return 1
    fi
    
    # Resolve server IP for WireGuard endpoint
    local server_ip
    server_ip=$(dig +short "$server" A | head -1)
    if [[ -z "$server_ip" ]]; then
        log "ERROR: Could not resolve $server"
        return 1
    fi
    
    # Generate config from template (replace {{ENDPOINT}} placeholder)
    sed "s/{{ENDPOINT}}/${server_ip}/g" "$CONFIG_TEMPLATE" > "$CONFIG_PATH"
    
    log "Applied config with endpoint: $server ($server_ip)"
    
    # Restart WireGuard interface
    log "Restarting WireGuard interface ${WIREGUARD_INTERFACE}..."
    if systemctl is-active --quiet "wg-quick@${WIREGUARD_INTERFACE}"; then
        systemctl restart "wg-quick@${WIREGUARD_INTERFACE}"
    else
        systemctl start "wg-quick@${WIREGUARD_INTERFACE}"
    fi
    
    save_current_server "$server"
    log "Successfully switched to $server"
}

# Main logic
main() {
    log "Starting Mullvad failover check..."
    
    local current_server
    current_server=$(get_current_server)
    
    # Check if current server is healthy
    if [[ -n "$current_server" ]]; then
        log "Current server: $current_server"
        local current_latency
        current_latency=$(test_server "$current_server")
        
        if [[ "$current_latency" -lt "$MAX_LATENCY_MS" ]]; then
            log "Current server healthy (${current_latency}ms), no action needed"
            return 0
        fi
        
        log "Current server unhealthy (${current_latency}ms), finding better option..."
    else
        log "No current server configured, finding best option..."
    fi
    
    # Find and apply best server
    local best_server
    if best_server=$(find_best_server); then
        if [[ "$best_server" != "$current_server" ]]; then
            apply_config "$best_server"
        else
            log "Best server is already current (but was temporarily slow)"
            save_current_server "$best_server"
        fi
    else
        log "CRITICAL: All Mullvad servers unreachable!"
        return 1
    fi
}

main "$@"
