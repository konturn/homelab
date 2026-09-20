# Observability

The observability stack covers three pillars — **logs**, **metrics**, and **traces** — plus **alerting** via Grafana.

## Architecture

```mermaid
graph TB
    subgraph sources [" Data Sources "]
        direction LR
        docker["Docker Containers"]
        system["System (host)"]
        openclaw["OpenClaw Gateway"]
        gitlab["GitLab"]
        other["Other Services"]
    end

    subgraph collectors [" Collectors "]
        direction LR
        promtail["Promtail"]
        telegraf["Telegraf"]
        otel["otel-collector\n:4317 gRPC · :4318 HTTP"]
    end

    subgraph backends [" Storage Backends "]
        direction LR
        loki["Loki :3100\nLogs"]
        influx["InfluxDB :8086\nMetrics"]
        tempo["Tempo :3200\nTraces"]
    end

    subgraph viz [" Visualization "]
        grafana["Grafana :3000\nDashboards · Alerting · Explore"]
    end

    %% Source to collector
    docker -- logs --> promtail
    system -- metrics --> telegraf
    other -- metrics --> telegraf
    openclaw -- OTLP --> otel
    gitlab -- OTLP --> otel

    %% Collector to backend
    promtail --> loki
    telegraf --> influx
    otel -- traces --> tempo
    otel -- metrics --> influx

    %% Backend to viz
    loki --> grafana
    influx --> grafana
    tempo --> grafana
```

## Signal Routing

| Signal  | Source             | Collector         | Backend    | Query Language |
|---------|--------------------|-------------------|------------|----------------|
| Logs    | Docker containers  | Promtail          | Loki       | LogQL          |
| Metrics | System, services   | Telegraf           | InfluxDB   | Flux           |
| Metrics | OTLP-instrumented  | otel-collector     | InfluxDB   | Flux           |
| Traces  | OTLP-instrumented  | otel-collector     | Tempo      | TraceQL        |

## Components

### Logs: Promtail → Loki

**Promtail** scrapes Docker container logs via the Docker logging driver and ships them to **Loki**.

- Config: `docker/promtail/config.yml` (router), `docker/promtail/satellite-config.yml` (satellites)
- All containers use the default `json-file` log driver (Promtail reads from `/var/lib/docker/containers`)
- Loki config: `docker/loki/local-config.yaml`
- Query via Grafana Explore with LogQL

#### Dropped lines

Some containers emit high-volume lines that carry no information. Promtail drops
them in the `docker` job's `pipeline_stages` before they reach Loki. Each rule
sets a `drop_counter_reason`, so what was discarded is still countable via
promtail's `promtail_dropped_lines_total` metric.

| Reason | Container | What it drops | Measured volume |
|--------|-----------|---------------|-----------------|
| `gitlab_tail_banner`      | gitlab | `==> /var/log/gitlab/<file> <==` banners from omnibus `tail -F` | 11.5k/hr (2026-09-11) |
| `gitlab_blank_line`       | gitlab | blank lines from the same `tail -F` | 11.4k/hr (2026-09-11) |
| `loki_compaction_chatter` | loki   | `index_set.go` / `tables_manager.go` info lines | 160k/day (2026-09-20) |
| `loki_listed_files`       | loki   | `table.go` `msg="listed files"` info lines | 33k/day (2026-09-20) |
| `loki_owned_streams`      | loki   | `recalculate owned streams` info lines | 5.8k/day (2026-09-20) |

The `loki_*` rules are anchored on `level=info`, so a warn or error from the same
caller still ships. Before adding a rule here, measure the line's share of total
ingest rather than guessing: `sum(count_over_time({job=~".+"}[24h]))` is the
denominator.

### Metrics: Telegraf → InfluxDB

**Telegraf** collects system and service metrics via plugins and writes to **InfluxDB v2**.

- Config: `docker/telegraf/telegraf.conf`
- Satellite config: `docker/telegraf/satellite-telegraf.conf`
- Org: `homelab`, Bucket: `metrics`
- ~77 input plugin instances covering Docker, system, SNMP, services, etc.
- Query via Grafana dashboards with Flux

### Traces + OTLP Metrics: otel-collector → Tempo / InfluxDB

The **OpenTelemetry Collector** (contrib distribution) receives OTLP data and routes it:

- **Traces** → Tempo (via OTLP/gRPC)
- **Metrics** → InfluxDB (via native InfluxDB exporter)
- **Logs** → debug exporter (stdout, for troubleshooting)

Config: `docker/otel-collector/config.yaml`

#### Instrumented Services

| Service   | Method                           | Signals          |
|-----------|----------------------------------|------------------|
| OpenClaw  | Native `diagnostics-otel` plugin | Traces + Metrics |
| GitLab    | Ruby OTel SDK (env vars)         | Traces           |
| Grafana   | Built-in OTel support            | Traces           |

### Alerting: Grafana

Grafana evaluates alert rules against all three backends and routes notifications.

- Alert rules: `docker/grafana/provisioning/alerting/infrastructure.yml`
- Contact points: `docker/grafana/provisioning/alerting/contactpoints.yml.j2`
- Notification policies: `docker/grafana/provisioning/alerting/policies.yml`

## Configuration Reference

| Component       | Config File                                    | Port(s)          |
|-----------------|------------------------------------------------|------------------|
| Grafana         | Env vars in `docker-compose.yml`               | 3000             |
| Loki            | `docker/loki/local-config.yaml`                | 3100             |
| Tempo           | `docker/tempo/tempo.yaml`                      | 3200, 4317, 4318 |
| InfluxDB        | Env vars in `docker-compose.yml`               | 8086             |
| Telegraf        | `docker/telegraf/telegraf.conf`                 | —                |
| Promtail        | `docker/promtail/config.yml`                   | —                |
| otel-collector  | `docker/otel-collector/config.yaml`            | 4317, 4318       |

## Adding Instrumentation

### OTLP traces (any service)

Set these environment variables in the service's compose definition:

```yaml
environment:
  - OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318
  - OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
  - OTEL_SERVICE_NAME=your-service-name
  - OTEL_TRACES_EXPORTER=otlp
```

The service must include an OTel SDK or auto-instrumentation library. This works out of the box for:
- **Node.js** — `@opentelemetry/auto-instrumentations-node`
- **Python** — `opentelemetry-distro`
- **Ruby** — `opentelemetry-sdk` (GitLab bundles this)
- **Go** — manual SDK integration required

### OpenClaw native plugin

OpenClaw has built-in OTLP export via `diagnostics-otel`. Configured in `openclaw.json.j2`:

```json
{
  "diagnostics": {
    "enabled": true,
    "otel": {
      "enabled": true,
      "endpoint": "http://otel-collector:4318",
      "serviceName": "openclaw-gateway",
      "traces": true,
      "metrics": true
    }
  },
  "plugins": {
    "allow": ["diagnostics-otel"],
    "entries": {
      "diagnostics-otel": { "enabled": true }
    }
  }
}
```

See: https://docs.openclaw.ai/logging#export-to-opentelemetry

### Telegraf metrics (non-OTLP)

Add input plugins to `docker/telegraf/telegraf.conf`. Output is pre-configured for InfluxDB.
