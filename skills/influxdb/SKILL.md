---
name: influxdb
description: InfluxDB v2 time series database. Write metrics, query with Flux, manage buckets.
---

# InfluxDB Skill

Interact with InfluxDB v2 for time series data, metrics, and monitoring.

## Configuration

Environment variables (already configured):
- `INFLUXDB_URL`: http://influxdb:8086
- `INFLUXDB_TOKEN`: Authorization token

**Organization:** homelab

**Buckets:**
- `metrics` - Main metrics storage (no expiry)
- `_monitoring` - System monitoring (7 day retention)
- `_tasks` - Task execution logs (3 day retention)

## Authentication

Include in all requests:
```
Authorization: Token $INFLUXDB_TOKEN
```

## Health Check

```bash
curl -s "$INFLUXDB_URL/health" | jq '{status, version}'
```

## Writing Data

### Line Protocol Format

```
measurement,tag1=value1,tag2=value2 field1=value1,field2=value2 timestamp
```

### Write Single Point

```bash
curl -s -X POST "$INFLUXDB_URL/api/v2/write?org=homelab&bucket=metrics&precision=s" \
  -H "Authorization: Token $INFLUXDB_TOKEN" \
  -H "Content-Type: text/plain" \
  -d "cpu_usage,host=server1,region=us value=65.5 $(date +%s)"
```

### Write Multiple Points

```bash
curl -s -X POST "$INFLUXDB_URL/api/v2/write?org=homelab&bucket=metrics&precision=s" \
  -H "Authorization: Token $INFLUXDB_TOKEN" \
  -H "Content-Type: text/plain" \
  -d "temperature,room=office value=72.5 $(date +%s)
temperature,room=bedroom value=68.2 $(date +%s)
humidity,room=office value=45 $(date +%s)"
```

## Querying Data (Flux)

### Basic Query

```bash
curl -s -X POST "$INFLUXDB_URL/api/v2/query?org=homelab" \
  -H "Authorization: Token $INFLUXDB_TOKEN" \
  -H "Content-Type: application/vnd.flux" \
  -H "Accept: application/csv" \
  -d 'from(bucket: "metrics")
  |> range(start: -1h)
  |> filter(fn: (r) => r._measurement == "cpu_usage")
  |> limit(n: 10)'
```

### Query with Aggregation

```bash
curl -s -X POST "$INFLUXDB_URL/api/v2/query?org=homelab" \
  -H "Authorization: Token $INFLUXDB_TOKEN" \
  -H "Content-Type: application/vnd.flux" \
  -H "Accept: application/csv" \
  -d 'from(bucket: "metrics")
  |> range(start: -24h)
  |> filter(fn: (r) => r._measurement == "temperature")
  |> aggregateWindow(every: 1h, fn: mean)
  |> yield(name: "hourly_avg")'
```

### List Measurements

```bash
curl -s -X POST "$INFLUXDB_URL/api/v2/query?org=homelab" \
  -H "Authorization: Token $INFLUXDB_TOKEN" \
  -H "Content-Type: application/vnd.flux" \
  -H "Accept: application/csv" \
  -d 'import "influxdata/influxdb/schema"
schema.measurements(bucket: "metrics")'
```

### Get Latest Value

```bash
curl -s -X POST "$INFLUXDB_URL/api/v2/query?org=homelab" \
  -H "Authorization: Token $INFLUXDB_TOKEN" \
  -H "Content-Type: application/vnd.flux" \
  -H "Accept: application/csv" \
  -d 'from(bucket: "metrics")
  |> range(start: -1d)
  |> filter(fn: (r) => r._measurement == "temperature")
  |> last()'
```

## Bucket Management

### List Buckets

```bash
curl -s "$INFLUXDB_URL/api/v2/buckets" \
  -H "Authorization: Token $INFLUXDB_TOKEN" | jq '.buckets[] | {name, id}'
```

### Get Bucket Details

```bash
curl -s "$INFLUXDB_URL/api/v2/buckets?name=metrics" \
  -H "Authorization: Token $INFLUXDB_TOKEN" | jq '.buckets[0]'
```

## Organization

### List Organizations

```bash
curl -s "$INFLUXDB_URL/api/v2/orgs" \
  -H "Authorization: Token $INFLUXDB_TOKEN" | jq '.orgs[] | {id, name}'
# homelab org ID: 545869d8522fa417
```

## Common Flux Patterns

### Filter by Tag

```flux
from(bucket: "metrics")
  |> range(start: -1h)
  |> filter(fn: (r) => r.host == "server1")
```

### Multiple Filters

```flux
from(bucket: "metrics")
  |> range(start: -1h)
  |> filter(fn: (r) => r._measurement == "cpu" and r.host == "server1")
  |> filter(fn: (r) => r._field == "usage_percent")
```

### Downsample Data

```flux
from(bucket: "metrics")
  |> range(start: -7d)
  |> aggregateWindow(every: 1h, fn: mean)
```

### Calculate Rate of Change

```flux
from(bucket: "metrics")
  |> range(start: -1h)
  |> derivative(unit: 1m, nonNegative: true)
```

## Common Workflows

### "Store a sensor reading"
```bash
curl -s -X POST "$INFLUXDB_URL/api/v2/write?org=homelab&bucket=metrics&precision=s" \
  -H "Authorization: Token $INFLUXDB_TOKEN" \
  -H "Content-Type: text/plain" \
  -d "sensor,location=kitchen,type=temperature value=72.5 $(date +%s)"
```

### "Get last hour of data"
```bash
curl -s -X POST "$INFLUXDB_URL/api/v2/query?org=homelab" \
  -H "Authorization: Token $INFLUXDB_TOKEN" \
  -H "Content-Type: application/vnd.flux" \
  -H "Accept: application/csv" \
  -d 'from(bucket: "metrics") |> range(start: -1h)'
```

### "Calculate daily average"
```bash
curl -s -X POST "$INFLUXDB_URL/api/v2/query?org=homelab" \
  -H "Authorization: Token $INFLUXDB_TOKEN" \
  -H "Content-Type: application/vnd.flux" \
  -H "Accept: application/csv" \
  -d 'from(bucket: "metrics")
  |> range(start: -1d)
  |> aggregateWindow(every: 1d, fn: mean)'
```

## Notes

- Version: v2.8.0
- Precision options: ns, us, ms, s (default: ns)
- Flux is the query language (InfluxQL deprecated in v2)
- CSV output is easier to parse than JSON for queries
- Timestamps are Unix epoch (nanoseconds by default)
