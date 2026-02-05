# Aruba S2500 Switch Configuration Deployment — Design Document

**Date:** 2026-02-04  
**Author:** Prometheus (automated research)  
**Status:** Draft — awaiting review before implementation

---

## 1. Executive Summary

This document designs a CI/CD pipeline step to deploy running configuration to an Aruba S2500-48P switch (ArubaOS 7.4) as part of the homelab GitLab CI deploy process. The config file at `networking/switches/aruba-config` is a Jinja2 template that needs to be resolved, deployed to the switch at 10.100.0.1, and verified.

**Recommendation:** Use `community.network.aruba_config` with `src` parameter via Ansible, with a phased approach: start with an incremental line-by-line config push (not full replace), add backup/rollback safety, and keep the deploy job **manual** until confidence is established.

---

## 2. Background & Current State

### Infrastructure Context
- **Switch:** Aruba S2500-48P, ArubaOS 7.4
- **Management IP:** 10.100.0.1 (on mgmt interface)
- **Router:** 10.100.0.2 on same L2 segment (`enp2s0f1` on router, per netplan)
- **SSH:** Enabled with `ssh mgmt-auth public-key`
- **Current management:** Manual SSH — no automation exists today
- **Config file:** `networking/switches/aruba-config` — Jinja2 template using `{{ lookup('env', 'ARUBA_CONFIG_SECRET') }}`

### Current CI Pipeline
The existing `.gitlab-ci.yml` deploys to three targets (router, zwave, satellite-2) using Ansible with:
- **Stages:** validate → build → deploy
- **Pattern:** Path-based change detection triggers specific jobs
- **Connection:** SSH with `ROUTER_PRIVATE_KEY_BASE64` secret
- **Ansible image:** `willhallonline/ansible:latest`

### Config File Structure
The `aruba-config` file is a full running-config dump (~280 lines) containing:
- VLANs 1-7 (external, LAN, management, guest, buster, IoT)
- Interface assignments (48 GigE ports + uplinks)
- Switching profiles (trunk-main, access VLANs)
- LACP port-channel config
- Spanning-tree (MSTP)
- SNMP community
- NTP server
- Management interface IP (10.100.0.1/24)
- SSH + mgmt-user authentication
- `enable secret` (templated via Jinja2)

---

## 3. Research Findings

### 3.1 Ansible Modules for ArubaOS

There are **three** relevant Ansible approaches for ArubaOS switches:

#### Option A: `community.network.aruba_config` (Recommended)
- **Collection:** `community.network` (install via `ansible-galaxy collection install community.network`)
- **Connection type:** `network_cli` (newer) or `local` (legacy)
- **Network OS:** `community.network.aruba` or just `aruba`
- **Capabilities:**
  - `lines` — push individual config lines
  - `src` — push a config file (template rendered on Ansible control host)
  - `parents` — specify config hierarchy context
  - `backup` — backup current running-config before changes
  - `save_when` — control when to write to startup-config
  - `diff_against` — compare running vs intended config
  - `match` — line, strict, exact, or none
- **Known issues:**
  - Documented bug with Aruba Mobility Controllers (ArubaOS 6.x/8.x) — SSH timeout issues (#53794). S2500 runs ArubaOS 7.4 which may or may not have the same issues.
  - **DEPRECATION WARNING:** `community.network` is deprecated and will be removed in Ansible 12 / collection v6.0.0. No official replacement is named for the Aruba modules.
  - `connection: local` is legacy; prefer `connection: network_cli`

#### Option B: `arubanetworks.aos_switch` collection
- **Collection:** `arubanetworks.aos_switch` (Aruba's official)
- **Modules:** `arubaoss_command`, `arubaoss_config`
- **Connection:** `network_cli`
- **Network OS:** `arubanetworks.aos_switch.arubaoss`
- **Concern:** This collection targets AOS-Switch (ProCurve/2930F/3810M etc.) — NOT the Mobility Access Switch (S2500). The S2500 runs a different OS variant (ArubaOS 7.x for mobility switches) vs AOS-Switch (16.x for wired switches). **May not be compatible.**

#### Option C: Raw SSH/expect scripts
- Use `ansible.builtin.raw` or `ansible.netcommon.cli_command` / `cli_config`
- Most universal approach — works regardless of module compatibility
- Requires careful handling of SSH prompts, enable mode, pager disabling
- `cli_config` from `ansible.netcommon` is platform-agnostic and works over `network_cli`

### 3.2 ArubaOS 7.4 CLI Patterns

Based on research, the S2500 (ArubaOS 7.x Mobility Access Switch) supports:
- `show running-config` — display current config
- `configure terminal` — enter config mode
- `write memory` — save running-config to startup-config
- `copy running-config tftp <host> <filename>` — backup to TFTP
- `copy running-config flash: <filename>` — backup to flash
- `copy tftp: <host> <filename> flash: <destfile>` — restore from TFTP to flash
- `copy scp: <host> <user> <filename> flash: <destfile>` — restore via SCP
- Config is line-by-line (similar to Cisco IOS style)
- Supports `no <command>` to remove configuration lines

### 3.3 REST API Availability

**ArubaOS 7.4 does NOT have a REST API.** REST API was introduced in:
- AOS-Switch 16.x (for wired switches like 2930F)
- ArubaOS-CX 10.x (for CX switches)
- ArubaOS 8.x added some REST API for Mobility Controllers

The S2500 on ArubaOS 7.4 is CLI-only. SSH is the only remote management option.

### 3.4 Full Replace vs Incremental

**Full config replace is NOT natively supported on ArubaOS 7.4.** Unlike Junos or IOS-XE, there's no `configure replace` command. Options:

1. **Incremental (line-by-line diff):** Ansible's `aruba_config` with `src` does this — it diffs the desired config against running-config and pushes only the changed lines. This is the **safest approach**.

2. **Full replace via TFTP/SCP:** You could SCP a config file to flash and reboot, but this is dangerous:
   - Requires reboot to take effect
   - No atomic rollback
   - Could brick the switch if config is malformed

3. **Wipe and reapply:** Clear running-config and push entire config. Extremely dangerous — would drop all connectivity mid-process.

**Recommendation: Incremental (line-by-line) is the only safe approach.**

---

## 4. Recommended Approach

### 4.1 Architecture

```
┌──────────────────────────────────────────────────┐
│                 GitLab CI Pipeline                │
│                                                   │
│  ┌────────────┐  ┌────────────┐  ┌────────────┐ │
│  │ aruba:val  │  │ router:val │  │ ... other  │ │
│  │ (check)    │  │            │  │  jobs      │ │
│  └─────┬──────┘  └────────────┘  └────────────┘ │
│        │                                          │
│  ┌─────▼──────┐                                  │
│  │aruba:deploy│ (manual or auto on main)         │
│  │            │                                   │
│  │ 1. Template config (Jinja2)                   │
│  │ 2. Backup current running-config              │
│  │ 3. Push incremental changes via SSH           │
│  │ 4. Save to startup-config                     │
│  │ 5. Verify: show running-config                │
│  └────────────┘                                  │
│        │                                          │
│  ┌─────▼──────┐                                  │
│  │ Switch     │ 10.100.0.1                       │
│  │ S2500-48P  │ mgmt interface                   │
│  └────────────┘                                  │
└──────────────────────────────────────────────────┘
```

### 4.2 Approach: Ansible with `community.network.aruba_config`

**Primary approach:** Use `community.network.aruba_config` module with `src` parameter.

**Fallback approach:** If the module has SSH compatibility issues with ArubaOS 7.4 on the S2500, fall back to `ansible.netcommon.cli_config` or raw SSH commands.

### 4.3 Ansible Playbook Design

New file: `ansible/aruba-switch.yml`

```yaml
---
- name: Deploy configuration to Aruba S2500 switch
  hosts: aruba_switch
  connection: network_cli
  gather_facts: no

  vars:
    config_src: "{{ lookup('env', 'CI_PROJECT_DIR') + '/networking/switches/aruba-config' }}"
    backup_dir: "/tmp/aruba-backup"

  tasks:
    - name: Ensure backup directory exists
      delegate_to: localhost
      ansible.builtin.file:
        path: "{{ backup_dir }}"
        state: directory

    - name: Backup current running configuration
      community.network.aruba_config:
        backup: yes
        backup_options:
          dir_path: "{{ backup_dir }}"
          filename: "aruba-s2500-backup-{{ ansible_date_time.iso8601_basic_short | default('unknown') }}.cfg"
      register: backup_result

    - name: Deploy configuration from template
      community.network.aruba_config:
        src: "{{ config_src }}"
        match: line
        replace: line
        save_when: changed
      register: deploy_result
      diff: yes

    - name: Display changes made
      ansible.builtin.debug:
        msg: "Commands pushed: {{ deploy_result.commands | default([]) }}"
      when: deploy_result.changed

    - name: Verify configuration - check hostname
      community.network.aruba_command:
        commands:
          - show running-config | include hostname
      register: verify_hostname

    - name: Verify configuration - check mgmt IP
      community.network.aruba_command:
        commands:
          - show interface mgmt
      register: verify_mgmt

    - name: Assert critical config elements
      ansible.builtin.assert:
        that:
          - "'ArubaS2500-48P-US' in verify_hostname.stdout[0]"
          - "'10.100.0.1' in verify_mgmt.stdout[0]"
        fail_msg: "Critical configuration verification failed! Check switch state immediately."
        success_msg: "Configuration verified successfully."
```

### 4.4 Inventory Addition

Add to `ansible/inventory.yml`:

```yaml
    aruba_switch:
      ansible_host: 10.100.0.1
      ansible_network_os: community.network.aruba
      ansible_connection: network_cli
      ansible_user: admin
      # SSH key auth — uses same key as router deployments
      ansible_ssh_private_key_file: tmp
      ansible_become: yes
      ansible_become_method: enable
      ansible_become_password: "{{ lookup('env', 'ARUBA_CONFIG_SECRET') }}"
```

**Note on hierarchy:** The S2500 switch likely needs to be a separate host entry, NOT under the `children: satellites:` group. It should be at the same level as `router.lab.nkontur.com`.

### 4.5 CI Pipeline Changes

Add to `.gitlab-ci.yml`:

```yaml
# Path-based change detection for Aruba switch
.aruba_switch_changes:
  rules:
    - if: $CI_PIPELINE_SOURCE == "merge_request_event"
      changes:
        - ansible/aruba-switch.yml
        - ansible/inventory.yml
        - networking/switches/aruba-config
        - requirements.txt
        - ansible/requirements.yml
        - .gitlab-ci.yml

# Validation stage (dry-run)
aruba-switch:validate:
  extends:
    - .ansible
    - .interruptible
    - .aruba_switch_changes
  stage: validate
  needs: []
  script:
    - ansible-galaxy collection install community.network
    - ansible-playbook -i ansible/inventory.yml --private-key=tmp ansible/aruba-switch.yml --check --diff

# Deploy stage (MANUAL for safety)
aruba-switch:deploy:
  extends: .ansible
  stage: deploy
  resource_group: deploy-aruba-switch
  needs:
    - job: aruba-switch:validate
      optional: true
  script:
    - ansible-galaxy collection install community.network
    - ansible-playbook -i ansible/inventory.yml --private-key=tmp ansible/aruba-switch.yml --diff
  rules:
    - if: $CI_COMMIT_BRANCH == "main"
      changes:
        - ansible/aruba-switch.yml
        - ansible/inventory.yml
        - networking/switches/aruba-config
        - .gitlab-ci.yml
      when: manual
      allow_failure: true
```

**Key design decisions:**
- `when: manual` — Requires human click to deploy. Switch config changes should be deliberate.
- `resource_group: deploy-aruba-switch` — Prevents concurrent switch deploys.
- `community.network` collection is installed at runtime (not cached) since it's small.

---

## 5. Safety Considerations

### 5.1 Rollback Strategy

**If config deployment breaks connectivity:**

1. **Automatic recovery (best case):** If we only modify running-config and don't `write memory`, a switch reboot will restore the startup-config. However, the module's `save_when: changed` WILL persist to startup-config.

2. **Manual recovery options:**
   - Physical console access (serial port on the S2500)
   - Wait 30 minutes — if the switch loses its mgmt IP, you'd need console access
   - The backup file from the pipeline job artifacts can be used to restore

3. **Proposed safeguards:**
   - **Always backup before changes** (done in playbook)
   - **Store backup as CI artifact** so it's downloadable from GitLab
   - **Manual deploy trigger** — no auto-deploy on merge
   - **Verify critical config** after deploy (hostname, mgmt IP, VLAN assignments)
   - **CI job timeout** — if SSH hangs (likely if we broke connectivity), job will fail and alert

4. **"Dead man's switch" approach (future enhancement):**
   - Push config to running-config only (don't save to startup)
   - Wait 5 minutes, verify connectivity
   - If connectivity confirmed, then `write memory`
   - If connectivity lost, reboot switch (via IPMI/PoE cycle/timer) to restore startup-config
   - This is complex and should be a future iteration

### 5.2 What Could Go Wrong

| Scenario | Impact | Mitigation |
|----------|--------|------------|
| Bad VLAN assignment breaks mgmt connectivity | Complete loss of switch management | Backup; manual deploy; console access |
| Malformed config line | Single command rejected, rest continues | Line-by-line mode will skip invalid lines |
| SSH timeout during deploy | Partial config applied | `resource_group` prevents concurrent; backup available |
| Enable secret changes | Locked out of privileged mode | Verify `ARUBA_CONFIG_SECRET` CI variable matches |
| LACP/port-channel change drops trunk | Loss of inter-VLAN routing | Test in check mode first; verify post-deploy |
| Removing mgmt interface IP | Immediate loss of SSH | `assert` task verifies mgmt IP; check mode catches this |

### 5.3 Idempotency

`aruba_config` with `src` is inherently idempotent:
- It diffs desired config vs running-config
- Only pushes lines that differ
- Re-running with no changes = no commands sent
- `save_when: changed` only writes to startup-config if running-config actually changed

**Caveat:** Some ArubaOS commands auto-modify their syntax (e.g., `enable secret` gets hashed). The `encrypt` parameter on `aruba_config` (defaults to `true`) handles this by comparing encrypted forms. If the enable secret is stored as a hash in the template, it should match; if it's plaintext, the module should handle the translation. **This needs testing.**

### 5.4 Config Template Concerns

The current `aruba-config` template contains:
```
enable secret {{ lookup('env', 'ARUBA_CONFIG_SECRET') }}
```

**Issue:** When `aruba_config` compares the `src` file against `show running-config`, the running config will have the hashed version of the enable secret, while the template will have the plaintext version. This could cause the module to try to re-apply the enable secret every run.

**Solutions:**
1. Store the hashed enable secret in the template (preferred for idempotency)
2. Use `diff_ignore_lines` to skip the enable secret line
3. Add `encrypt: false` to expose plaintext for comparison (security concern)

**Recommendation:** Use option 2 initially — add `diff_ignore_lines: ['^enable secret']` to the task.

---

## 6. Credentials & Access Required

### 6.1 New CI/CD Variables Needed

| Variable | Purpose | Type |
|----------|---------|------|
| `ARUBA_CONFIG_SECRET` | Enable secret for the switch | Protected, Masked |
| `ARUBA_SSH_USER` | SSH username (likely `admin`) | Protected |

**Note:** The `ROUTER_PRIVATE_KEY_BASE64` may work for the switch too if the same SSH key is authorized. The switch has `ssh mgmt-auth public-key` enabled, so the CI runner's SSH key needs to be added to the switch's authorized keys.

### 6.2 SSH Key Setup

The CI pipeline currently uses `ROUTER_PRIVATE_KEY_BASE64` for the router. For the switch:

1. Extract the public key from the CI runner's private key
2. SSH to the switch and add the key:
   ```
   (S2500) # configure terminal
   (S2500) (config) # mgmt-user admin root <password-hash>
   (S2500) (config) # pubkey-pem <paste public key>
   ```
3. Test SSH connectivity from the CI runner network to 10.100.0.1

**Connectivity path:** GitLab Runner → Router (10.100.0.2) → Switch (10.100.0.1) on the mgmt L2 segment. The runner must have a route to 10.100.0.0/24.

### 6.3 SSH known_hosts

Add the switch's SSH host key to `ansible/known_hosts`:
```bash
ssh-keyscan -t ed25519,rsa 10.100.0.1 >> ansible/known_hosts
```

---

## 7. Testing Plan

### Phase 1: Validate SSH Connectivity
- [ ] Verify CI runner can reach 10.100.0.1
- [ ] Verify SSH key authentication works
- [ ] Verify Ansible can connect with `network_cli` and `ansible_network_os: aruba`
- [ ] Run: `ansible -m ping -i inventory.yml aruba_switch` (will likely fail — network devices don't support ping module; use `community.network.aruba_command` with `show version` instead)

### Phase 2: Read-Only Test
- [ ] Run playbook with `--check --diff` to see what would change
- [ ] Verify backup task works (downloads running-config)
- [ ] Verify `aruba_command` can run `show running-config`

### Phase 3: Non-Destructive Change
- [ ] Make a trivial config change (e.g., update NTP server or add a description)
- [ ] Deploy via CI with manual trigger
- [ ] Verify change applied
- [ ] Verify idempotency (re-run shows no changes)

### Phase 4: Full Config Deployment
- [ ] Deploy the full config template
- [ ] Verify all VLANs, interfaces, profiles match
- [ ] Verify no connectivity disruption

### Phase 5: Promote to Auto-Deploy (Optional)
- [ ] After N successful manual deploys, consider switching to auto-deploy on main
- [ ] Keep `resource_group` to prevent concurrent deploys

---

## 8. Fallback: Raw SSH Approach

If `community.network.aruba_config` doesn't work with ArubaOS 7.4 on the S2500 (which is plausible given the known issues with Mobility Controllers), here's the fallback:

### Approach: `ansible.netcommon.cli_config` or raw SSH

```yaml
- name: Deploy config via cli_config
  ansible.netcommon.cli_config:
    config: "{{ lookup('template', config_src) }}"
    diff_match: line
    diff_replace: line
  register: deploy_result
```

Or using raw SSH expect script:

```yaml
- name: Deploy config via raw SSH
  ansible.builtin.shell: |
    #!/bin/bash
    set -e
    
    # Template the config
    CONFIG_FILE=$(mktemp)
    ansible-template ... > "$CONFIG_FILE"
    
    # Use sshpass/expect to push config
    ssh -o StrictHostKeyChecking=no admin@10.100.0.1 << 'EOF'
    enable
    configure terminal
    # ... push commands from diff ...
    write memory
    end
    exit
    EOF
  delegate_to: localhost
```

**Note:** This is significantly more fragile than using the Ansible network modules.

### Approach: Python expect script (most robust fallback)

```python
#!/usr/bin/env python3
"""Deploy config to Aruba S2500 via SSH with expect-like handling."""
import paramiko
import time
import sys
import difflib

def connect_switch(host, user, key_file):
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(host, username=user, key_filename=key_file)
    return client

def get_running_config(client):
    stdin, stdout, stderr = client.exec_command('show running-config')
    return stdout.read().decode()

def push_config_lines(client, lines):
    shell = client.invoke_shell()
    time.sleep(1)
    shell.send('enable\n')
    time.sleep(1)
    shell.send('configure terminal\n')
    time.sleep(1)
    for line in lines:
        shell.send(line + '\n')
        time.sleep(0.5)
    shell.send('end\n')
    time.sleep(1)
    shell.send('write memory\n')
    time.sleep(2)
    output = shell.recv(65535).decode()
    shell.close()
    return output

# ... diff computation, etc.
```

---

## 9. Open Questions

1. **Does `community.network.aruba_config` work with ArubaOS 7.4 on the S2500?** The module was designed for Aruba controllers and switches, but the S2500 runs a specific ArubaOS variant. Need to test.

2. **SSH key format:** Does the S2500's `ssh mgmt-auth public-key` accept OpenSSH ed25519 keys, or does it need RSA? The CI key is ed25519. May need an RSA key for older ArubaOS.

3. **CI runner connectivity:** Can the GitLab runner reach 10.100.0.1? The runner runs as a Docker container on the router, which has 10.100.0.2 on the mgmt interface. Should work, but needs verification.

4. **Enable secret format:** Is the `ARUBA_CONFIG_SECRET` value the plaintext password or the already-hashed value? The config shows `enable secret {{ lookup('env', 'ARUBA_CONFIG_SECRET') }}` followed by what appears to be a hash. Need to verify what the CI variable contains.

5. **`mgmt-user` password:** The config has `mgmt-user admin root 7a14201e01e17c119dc119a16dff522f24c600aa87d8036513` — is this the hashed admin password? If so, this is static and fine. If it changes per-deploy, it's a problem.

6. **Deprecation of community.network:** The collection is being removed from Ansible 12. We need a long-term plan. Options: pin the collection version, fork the module, or move to raw SSH.

---

## 10. Dependencies to Add

### `ansible/requirements.yml`
```yaml
collections:
  - name: community.network
    version: ">=5.0.0"
  - name: ansible.netcommon
    version: ">=5.0.0"
```

(Currently `requirements.yml` only has the restic role. Need to add collections.)

### `requirements.txt`
Add `paramiko` if not already included (needed for `network_cli` connection plugin):
```
ansible
setuptools
crossplane
paramiko
```

---

## 11. Summary & Next Steps

### Recommended Implementation Order

1. **Set up credentials:** Add `ARUBA_CONFIG_SECRET` to GitLab CI/CD variables. Verify SSH key access to switch.
2. **Add inventory entry:** Add `aruba_switch` host to `ansible/inventory.yml`
3. **Add collection dependency:** Update `ansible/requirements.yml` with `community.network`
4. **Create playbook:** `ansible/aruba-switch.yml` with backup → deploy → verify pattern
5. **Add CI jobs:** `aruba-switch:validate` and `aruba-switch:deploy` (manual) to `.gitlab-ci.yml`
6. **Test Phase 1-2:** SSH connectivity and read-only operations
7. **Test Phase 3:** Trivial change via CI
8. **Test Phase 4:** Full config deployment
9. **Document:** Update `CLAUDE.md` with switch deployment info

### Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Module incompatible with S2500 | Medium | Low (fall back to cli_config) | Test early, have fallback ready |
| Config change causes network outage | Low | High | Manual deploy, backup, verify |
| SSH key auth doesn't work | Medium | Low (add key to switch) | Pre-test connectivity |
| Enable secret mismatch causes idempotency issues | High | Low (cosmetic) | Use diff_ignore_lines |
| Deprecation of community.network | Certain (future) | Medium | Pin version, plan migration |

### This document is ready for Noah's review before any implementation begins.
