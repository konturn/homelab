# Edge quarantine

A reversible "down for maintenance" switch for services behind the external
nginx. Turning it on for a service makes its vhost (or a path prefix of a
shared vhost) answer every request with a static 503 page, and can also reject
new connections to a port forward. It never stops containers and never touches
application data; turning it off restores normal routing.

| Piece | Where |
|---|---|
| `edge-quarantine` (CLI) | `/usr/local/sbin/edge-quarantine` on the router |
| `nginx-safe-reload` (reload + post-reload check, also used by the deploy) | `/usr/local/sbin/nginx-safe-reload` |
| state (source of truth) | `/var/lib/edge-quarantine/state.json` (root, 0600) |
| rendered nginx map | `/persistent_data/application/nginx/conf/edge-quarantine/active.map` |
| nginx side | `map "$host$uri" $edge_quarantine` in `external_http.conf`; vhosts opt in with `include edge_quarantine;` (`docker/nginx/edge_quarantine`) |
| port rules | iptables chain `EDGE-QUARANTINE`, jumped to from the top of `FORWARD` |
| audit log | `/var/log/edge-quarantine.log` (JSON lines, 0600) and the journal (`-t edge-quarantine`) |
| config | `/etc/edge-quarantine/config.json` (written by the deploy) |

## Usage

```bash
edge-quarantine on blog --host blog.example.com --reason "pending patch" --actor noah
edge-quarantine on media --path example.com:/media/ --port eth0:tcp:32400 --reason ... --actor ...
edge-quarantine status
edge-quarantine verify            # every quarantined host/path still serves the 503 page
edge-quarantine off blog --actor noah --reason "patched"
```

Add `--json` (before the command) for machine-readable output. Exit codes:
0 ok or nothing to do, 1 error (nothing changed), 2 applied but verification
failed, 3 usage error.

`on`/`off` take a lock, write the map, reload nginx through
`nginx-safe-reload` (a rejected reload puts the previous map back and changes
nothing), update the state, sync the port rules and then check over HTTPS
(against `verify_addr`) that each host/path answers 503 with
`X-Edge-Quarantine: 1` (on) or without it (off). Running the same command
twice is a no-op. A change takes a few seconds.

## Persistence

- Deploys: the map lives outside the deployed files and is never cleaned; the
  deploy also runs `edge-quarantine render`, which rewrites the map and the
  port rules from the state file (no-op when they match).
- fw-apply: `EDGE-QUARANTINE` is treated as a foreign chain, so a firewall
  reload keeps the chain and its hook.
- Reboots: `edge-quarantine-restore.service` runs `render` at boot.

## Drill target

`edge-canary.lab.nkontur.com` on the external nginx serves a static
`edge-canary ok` page to LAN and tailnet clients only. Use it to exercise the
switch end to end:

```bash
edge-quarantine on canary --host edge-canary.lab.nkontur.com --reason drill --actor <you>
curl -sk --resolve edge-canary.lab.nkontur.com:443:10.2.32.1 -D - https://edge-canary.lab.nkontur.com/ -o /dev/null
edge-quarantine off canary --reason drill --actor <you>
```

Tests: `python3 -m unittest discover -s docker/nginx/edge-quarantine -v`.
