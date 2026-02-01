---
name: ipmi
description: IPMI server management - power control, sensor readings, SOL console. Requires network access to mgmt VLAN and credentials.
---

# IPMI Skill

Manage server hardware via IPMI (Intelligent Platform Management Interface).

## Requirements

**Network:**
- Access to mgmt VLAN (10.4.x.x)
- IPMI BMC at `10.4.128.7`

**Tools:**
- `ipmitool` CLI (not currently installed in container)

**Credentials needed:**
- `IPMI_HOST`: 10.4.128.7
- `IPMI_USER`: BMC username
- `IPMI_PASS`: BMC password

## Current Status

⚠️ **Not yet functional:**
- Container needs mgmt network access (MR !43)
- `ipmitool` not installed
- No IPMI credentials configured
- IPMI may not be physically connected (check switch port 0/0/0 or 0/0/1)

## Operations (once configured)

### Power Control

```bash
# Check power status
ipmitool -I lanplus -H $IPMI_HOST -U $IPMI_USER -P $IPMI_PASS power status

# Power on
ipmitool -I lanplus -H $IPMI_HOST -U $IPMI_USER -P $IPMI_PASS power on

# Power off (hard)
ipmitool -I lanplus -H $IPMI_HOST -U $IPMI_USER -P $IPMI_PASS power off

# Power cycle
ipmitool -I lanplus -H $IPMI_HOST -U $IPMI_USER -P $IPMI_PASS power cycle

# Soft shutdown (ACPI)
ipmitool -I lanplus -H $IPMI_HOST -U $IPMI_USER -P $IPMI_PASS power soft
```

### Sensor Readings

```bash
# All sensors
ipmitool -I lanplus -H $IPMI_HOST -U $IPMI_USER -P $IPMI_PASS sensor list

# Specific sensor
ipmitool -I lanplus -H $IPMI_HOST -U $IPMI_USER -P $IPMI_PASS sensor get "CPU Temp"

# Thresholds
ipmitool -I lanplus -H $IPMI_HOST -U $IPMI_USER -P $IPMI_PASS sensor thresh
```

### System Event Log

```bash
# View SEL
ipmitool -I lanplus -H $IPMI_HOST -U $IPMI_USER -P $IPMI_PASS sel list

# Clear SEL
ipmitool -I lanplus -H $IPMI_HOST -U $IPMI_USER -P $IPMI_PASS sel clear
```

### Serial Over LAN (SOL)

```bash
# Activate SOL console
ipmitool -I lanplus -H $IPMI_HOST -U $IPMI_USER -P $IPMI_PASS sol activate

# Deactivate
ipmitool -I lanplus -H $IPMI_HOST -U $IPMI_USER -P $IPMI_PASS sol deactivate
```

### Chassis Info

```bash
# Chassis status
ipmitool -I lanplus -H $IPMI_HOST -U $IPMI_USER -P $IPMI_PASS chassis status

# BMC info
ipmitool -I lanplus -H $IPMI_HOST -U $IPMI_USER -P $IPMI_PASS bmc info
```

## Web Interface

Supermicro IPMI typically has a web UI:
- URL: `http://10.4.128.7` or `https://10.4.128.7`
- Default port: 80 (HTTP) or 443 (HTTPS)

Can use curl for some API operations if web interface supports it.

## Setup TODO

1. [ ] Deploy MR !43 (mgmt network access for moltbot)
2. [ ] Plug IPMI into switch port 0/0/0 or 0/0/1 (VLAN 4)
3. [ ] Add `ipmitool` to moltbot container
4. [ ] Configure `IPMI_USER` and `IPMI_PASS` env vars
5. [ ] Verify connectivity: `curl -s http://10.4.128.7`
