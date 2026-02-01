---
name: grafana
description: Grafana dashboard and alerting management via API. Query dashboards, create alerts, explore metrics.
---

# Grafana Skill

Manage Grafana dashboards, alerts, and data sources via API.

## Configuration

Environment variables (configured):
- `GRAFANA_URL`: http://grafana:3000
- `GRAFANA_TOKEN`: Service account token with Editor role

## Authentication

Include in all requests:
```
Authorization: Bearer $GRAFANA_TOKEN
```

## Core Operations

### Health Check

```bash
curl -s "$GRAFANA_URL/api/health" \
  -H "Authorization: Bearer $GRAFANA_TOKEN" | jq .
```

### List Dashboards

```bash
curl -s "$GRAFANA_URL/api/search?type=dash-db" \
  -H "Authorization: Bearer $GRAFANA_TOKEN" | jq '.[] | {uid, title, url}'
```

### Get Dashboard by UID

```bash
curl -s "$GRAFANA_URL/api/dashboards/uid/{uid}" \
  -H "Authorization: Bearer $GRAFANA_TOKEN" | jq .
```

### Get Dashboard Panels

```bash
curl -s "$GRAFANA_URL/api/dashboards/uid/{uid}" \
  -H "Authorization: Bearer $GRAFANA_TOKEN" | jq '.dashboard.panels[] | {id, title, type}'
```

### Query Data Source (InfluxDB via Grafana)

```bash
# Get data sources
curl -s "$GRAFANA_URL/api/datasources" \
  -H "Authorization: Bearer $GRAFANA_TOKEN" | jq '.[] | {id, name, type}'

# Query via data source proxy
curl -s "$GRAFANA_URL/api/ds/query" \
  -H "Authorization: Bearer $GRAFANA_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "queries": [{
      "datasourceId": 1,
      "rawQuery": true,
      "query": "from(bucket: \"telegraf\") |> range(start: -1h) |> filter(fn: (r) => r._measurement == \"cpu\")"
    }],
    "from": "now-1h",
    "to": "now"
  }'
```

### List Alert Rules

```bash
curl -s "$GRAFANA_URL/api/v1/provisioning/alert-rules" \
  -H "Authorization: Bearer $GRAFANA_TOKEN" | jq '.[] | {uid, title, condition}'
```

### Create Alert Rule

```bash
curl -s -X POST "$GRAFANA_URL/api/v1/provisioning/alert-rules" \
  -H "Authorization: Bearer $GRAFANA_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "High CPU Alert",
    "ruleGroup": "system-alerts",
    "folderUID": "system",
    "condition": "C",
    "data": [...],
    "for": "5m",
    "annotations": {"summary": "CPU usage above 80%"},
    "labels": {"severity": "warning"}
  }'
```

### Get Alert Notifications (Contact Points)

```bash
curl -s "$GRAFANA_URL/api/v1/provisioning/contact-points" \
  -H "Authorization: Bearer $GRAFANA_TOKEN" | jq .
```

### Create Dashboard

```bash
curl -s -X POST "$GRAFANA_URL/api/dashboards/db" \
  -H "Authorization: Bearer $GRAFANA_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "dashboard": {
      "title": "New Dashboard",
      "panels": [],
      "schemaVersion": 30
    },
    "overwrite": false
  }'
```

### Update Dashboard

```bash
# Fetch existing, modify, push back
DASH=$(curl -s "$GRAFANA_URL/api/dashboards/uid/{uid}" -H "Authorization: Bearer $GRAFANA_TOKEN")
# Modify $DASH.dashboard as needed
curl -s -X POST "$GRAFANA_URL/api/dashboards/db" \
  -H "Authorization: Bearer $GRAFANA_TOKEN" \
  -H "Content-Type: application/json" \
  -d "$DASH"
```

## Best Practices

**For LLM usage:**
- Query raw data via InfluxDB skill directly for analysis (faster, no visualization layer)
- Use Grafana API for dashboard/alert management, not data exploration
- When debugging: query metrics directly, don't try to "see" dashboards

**Dashboard modifications:**
- Always fetch current version before updating (handles version conflicts)
- Test changes on a copy first if unsure
- Panel positioning is grid-based (x, y, w, h in grid units)

**Alerts:**
- Define clear, actionable alerts with good annotations
- Use appropriate `for` duration to avoid flapping
- Route to contact points that reach moltbot (webhook preferred)

## Common Workflows

### "Check what dashboards exist"
```bash
curl -s "$GRAFANA_URL/api/search?type=dash-db" \
  -H "Authorization: Bearer $GRAFANA_TOKEN" | jq '.[].title'
```

### "Add a panel to existing dashboard"
1. GET dashboard by UID
2. Add panel object to `.dashboard.panels[]`
3. POST back to `/api/dashboards/db`

### "Set up alert for metric"
1. Create alert rule via provisioning API
2. Ensure contact point exists for notifications
3. Test by triggering condition manually

## Existing Dashboards

- `IPMI Dash` — Server hardware monitoring
- `System Dashboard` — General system metrics
