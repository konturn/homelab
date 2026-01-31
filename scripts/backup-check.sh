#!/bin/bash
# backup-check.sh - Verify backup-critical data exists and is healthy
#
# This script checks:
# - Critical directories exist and have recent data
# - ZFS pool health
# - Disk space status
# - Docker volume existence
#
# Run periodically to ensure backup targets are healthy before backing up.

set -euo pipefail

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Counters
PASS=0
WARN=0
FAIL=0

# Configuration
PERSISTENT_DATA="/persistent_data/application"
MPOOL="/mpool"
MAX_AGE_DAYS=7  # Warn if no files modified in this many days

log_pass() {
    echo -e "${GREEN}✓${NC} $1"
    ((PASS++))
}

log_warn() {
    echo -e "${YELLOW}⚠${NC} $1"
    ((WARN++))
}

log_fail() {
    echo -e "${RED}✗${NC} $1"
    ((FAIL++))
}

check_dir_exists() {
    local dir="$1"
    local name="$2"
    
    if [[ -d "$dir" ]]; then
        log_pass "$name exists: $dir"
        return 0
    else
        log_fail "$name missing: $dir"
        return 1
    fi
}

check_dir_has_recent_data() {
    local dir="$1"
    local name="$2"
    local max_days="${3:-$MAX_AGE_DAYS}"
    
    if [[ ! -d "$dir" ]]; then
        log_fail "$name missing: $dir"
        return 1
    fi
    
    # Find files modified within max_days
    local recent_files
    recent_files=$(find "$dir" -type f -mtime -"$max_days" 2>/dev/null | head -1)
    
    if [[ -n "$recent_files" ]]; then
        log_pass "$name has recent data (within ${max_days}d)"
        return 0
    else
        log_warn "$name has no files modified in ${max_days} days"
        return 1
    fi
}

check_docker_volume() {
    local volume="$1"
    
    if docker volume inspect "$volume" &>/dev/null; then
        log_pass "Docker volume exists: $volume"
        return 0
    else
        log_fail "Docker volume missing: $volume"
        return 1
    fi
}

echo "======================================"
echo "  Homelab Backup Health Check"
echo "  $(date)"
echo "======================================"
echo ""

# ==========================================
# ZFS Pool Health
# ==========================================
echo "## ZFS Pool Health"
echo ""

if command -v zpool &>/dev/null; then
    # Check pool status
    pool_status=$(zpool status -x 2>/dev/null || echo "error")
    
    if [[ "$pool_status" == "all pools are healthy" ]]; then
        log_pass "ZFS pools are healthy"
    elif [[ "$pool_status" == "error" ]]; then
        log_warn "Could not check ZFS pool status (not running as root?)"
    else
        log_fail "ZFS pool issues detected:"
        echo "$pool_status" | head -20
    fi
    
    # Check pool capacity
    if zpool list -H -o name,capacity 2>/dev/null | while read -r pool cap; do
        cap_num=${cap%\%}
        if [[ "$cap_num" -gt 90 ]]; then
            log_fail "Pool $pool is ${cap} full (>90%)"
        elif [[ "$cap_num" -gt 80 ]]; then
            log_warn "Pool $pool is ${cap} full (>80%)"
        else
            log_pass "Pool $pool capacity: ${cap}"
        fi
    done; then
        :
    fi
else
    log_warn "zpool command not found - skipping ZFS checks"
fi

echo ""

# ==========================================
# Disk Space
# ==========================================
echo "## Disk Space"
echo ""

for mount in "/" "$PERSISTENT_DATA" "$MPOOL"; do
    if [[ -d "$mount" ]]; then
        usage=$(df -h "$mount" 2>/dev/null | awk 'NR==2 {print $5}' | tr -d '%')
        if [[ -n "$usage" ]]; then
            if [[ "$usage" -gt 90 ]]; then
                log_fail "$mount is ${usage}% full"
            elif [[ "$usage" -gt 80 ]]; then
                log_warn "$mount is ${usage}% full"
            else
                log_pass "$mount: ${usage}% used"
            fi
        fi
    fi
done

echo ""

# ==========================================
# Critical Directories (Tier 1)
# ==========================================
echo "## Critical Data (Tier 1 - Must Backup)"
echo ""

check_dir_exists "$PERSISTENT_DATA/bitwarden" "Bitwarden data"
check_dir_has_recent_data "$PERSISTENT_DATA/bitwarden" "Bitwarden"

check_dir_exists "$PERSISTENT_DATA/homeassistant" "Home Assistant config"
check_dir_has_recent_data "$PERSISTENT_DATA/homeassistant" "Home Assistant"

check_dir_exists "$PERSISTENT_DATA/gitlab" "GitLab data"
check_dir_has_recent_data "$PERSISTENT_DATA/gitlab" "GitLab"

check_dir_exists "$PERSISTENT_DATA/zigbee2mqtt" "Zigbee2MQTT data"
check_dir_has_recent_data "$PERSISTENT_DATA/zigbee2mqtt" "Zigbee2MQTT"

check_dir_exists "$PERSISTENT_DATA/certs" "SSL certificates"

check_dir_exists "$MPOOL/nextcloud/nextcloud" "Nextcloud files"
check_dir_has_recent_data "$MPOOL/nextcloud/nextcloud" "Nextcloud files" 30

check_dir_exists "$MPOOL/nextcloud/paperless" "Paperless documents"

echo ""

# ==========================================
# Important Directories (Tier 2)
# ==========================================
echo "## Important Data (Tier 2 - Should Backup)"
echo ""

for service in nginx lab_nginx iot_nginx pihole radarr sonarr prowlarr grafana influxdb moltbot mqtt paperless; do
    check_dir_exists "$PERSISTENT_DATA/$service" "$service config"
done

echo ""

# ==========================================
# Docker Volumes
# ==========================================
echo "## Docker Volumes"
echo ""

if command -v docker &>/dev/null; then
    for volume in nextcloud_db wordpress_db influxdb-storage grafana-storage mosquitto; do
        check_docker_volume "$volume"
    done
else
    log_warn "Docker not available - skipping volume checks"
fi

echo ""

# ==========================================
# Summary
# ==========================================
echo "======================================"
echo "  Summary"
echo "======================================"
echo ""
echo -e "${GREEN}Passed:${NC}  $PASS"
echo -e "${YELLOW}Warnings:${NC} $WARN"
echo -e "${RED}Failed:${NC}  $FAIL"
echo ""

if [[ $FAIL -gt 0 ]]; then
    echo -e "${RED}BACKUP CHECK FAILED${NC} - Address failures before backing up"
    exit 1
elif [[ $WARN -gt 0 ]]; then
    echo -e "${YELLOW}BACKUP CHECK PASSED WITH WARNINGS${NC}"
    exit 0
else
    echo -e "${GREEN}BACKUP CHECK PASSED${NC}"
    exit 0
fi
