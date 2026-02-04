---
name: portainer
description: Control Docker containers and stacks via Portainer API. List containers, start/stop/restart, view logs, and redeploy stacks.
---

# Portainer

Manage Docker infrastructure through Portainer's REST API.

## Configuration

Environment variables (need to configure):
- `PORTAINER_URL`: Portainer instance URL (e.g., `https://portainer:9443`)
- `PORTAINER_API_KEY`: API Access Token (My Account → Access tokens → Add)

## Authentication

Include in all requests:
```
X-API-Key: $PORTAINER_API_KEY
```

## Core Operations

### Check Server Status

```bash
curl -s -k "$PORTAINER_URL/api/status" -H "X-API-Key: $PORTAINER_API_KEY" | \
  jq '{version: .Version}'
```

### List Endpoints (Docker Environments)

```bash
curl -s -k "$PORTAINER_URL/api/endpoints" -H "X-API-Key: $PORTAINER_API_KEY" | \
  jq '.[] | {id: .Id, name: .Name, status: (if .Status == 1 then "online" else "offline" end)}'
```

### List Containers

```bash
# On specific endpoint (endpoint_id=1)
curl -s -k "$PORTAINER_URL/api/endpoints/1/docker/containers/json?all=true" \
  -H "X-API-Key: $PORTAINER_API_KEY" | \
  jq '.[] | {name: .Names[0], state: .State, status: .Status}'
```

### Start/Stop/Restart Container

```bash
# Start
curl -s -k -X POST "$PORTAINER_URL/api/endpoints/1/docker/containers/{container_id}/start" \
  -H "X-API-Key: $PORTAINER_API_KEY"

# Stop
curl -s -k -X POST "$PORTAINER_URL/api/endpoints/1/docker/containers/{container_id}/stop" \
  -H "X-API-Key: $PORTAINER_API_KEY"

# Restart
curl -s -k -X POST "$PORTAINER_URL/api/endpoints/1/docker/containers/{container_id}/restart" \
  -H "X-API-Key: $PORTAINER_API_KEY"
```

### View Container Logs

```bash
curl -s -k "$PORTAINER_URL/api/endpoints/1/docker/containers/{container_id}/logs?stdout=true&stderr=true&tail=100" \
  -H "X-API-Key: $PORTAINER_API_KEY"
```

### List Stacks

```bash
curl -s -k "$PORTAINER_URL/api/stacks" -H "X-API-Key: $PORTAINER_API_KEY" | \
  jq '.[] | {id: .Id, name: .Name, status: (if .Status == 1 then "active" else "inactive" end), endpoint: .EndpointId}'
```

### Get Stack Details

```bash
curl -s -k "$PORTAINER_URL/api/stacks/{stack_id}" -H "X-API-Key: $PORTAINER_API_KEY" | \
  jq '{name: .Name, status: .Status, endpoint: .EndpointId, git: .GitConfig}'
```

### Redeploy Stack (Pull from Git)

```bash
curl -s -k -X PUT "$PORTAINER_URL/api/stacks/{stack_id}/git/redeploy?endpointId={endpoint_id}" \
  -H "X-API-Key: $PORTAINER_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"prune": true, "pullImage": true}'
```

### Get Stack Compose File

```bash
curl -s -k "$PORTAINER_URL/api/stacks/{stack_id}/file" -H "X-API-Key: $PORTAINER_API_KEY" | \
  jq -r '.StackFileContent'
```

## Common Workflows

### "What's running?"
```bash
curl -s -k "$PORTAINER_URL/api/endpoints/1/docker/containers/json" \
  -H "X-API-Key: $PORTAINER_API_KEY" | \
  jq '.[] | select(.State == "running") | .Names[0]'
```

### "Restart a service"
1. List containers to get container ID
2. POST to `/containers/{id}/restart`

### "Deploy latest code"
1. PUT to `/stacks/{id}/git/redeploy` with pullImage: true
2. Check logs to verify startup

## Notes

- Use `-k` flag to skip SSL verification if self-signed cert
- Container IDs are long hashes, not names
- Stack redeployment pulls from git and rebuilds
- Endpoint ID 1 is typically the local Docker instance
