# Observability Scanner

This document explains how the ObservabilityScanner works and how it fits into the scale test monitoring pipeline.

## What It Does

The scanner is the "always-on" monitoring layer that runs alongside the existing monitor, anomaly detector, and health sweep. While the monitor tracks pod ready rate and the anomaly detector investigates after problems are detected, the scanner proactively queries Prometheus and CloudWatch to catch problems before they affect the pod ready rate.

Think of it as a smoke detector — it runs cheap fleet-wide queries every 15-30 seconds looking for early warning signs (CPU pressure, pending pod backlogs, Karpenter queue depth, network errors). When it finds something, it triggers a more expensive CloudWatch drill-down to get the specific error messages.

## How It Fits In

The scanner runs as a parallel, independent component. It does not replace or modify any existing module.

```mermaid
flowchart TB
    subgraph Controller["Scale Test Controller"]
        direction LR
        MON["Monitor\n(rate)"]
        AD["Anomaly\nDetector"]
        EW["Event\nWatcher"]
        SCAN["Observability\nScanner"]
    end

    MON ---|"Pod ready rate\nvia deployment\nwatch API"| K8S["K8s API"]
    AD ---|"Reactive investigation\nafter alerts"| SSM["SSM / EC2"]
    EW ---|"K8s Warning\nevent stream"| K8S
    SCAN ---|"Fleet-wide PromQL + CW\nqueries every 15-30s"| AMP["AMP / CloudWatch"]

    style Controller fill:#1a1a2e,stroke:#16213e,color:#e0e0e0
    style SCAN fill:#14532d,stroke:#1a7a3a,color:#e0e0e0
```

Each module owns its own data path:

| Module | What it owns | How it works |
|--------|-------------|--------------|
| monitor.py | Pod ready rate | Deployment watch API, 5s ticker loop |
| anomaly.py | Reactive investigation | K8s events → SSM → EC2 ENI state |
| health_sweep.py | Per-node health at peak | Per-node PromQL (`avg by(node)(...)`) |
| events.py | K8s event streaming | Watch API for Warning events |
| **observability.py** | **Fleet-wide proactive scans** | **Fleet-aggregated PromQL + CW Insights** |

The scanner's fleet-aggregated queries (`avg(...)`, `count(...)`, `sum(...)`) serve a different purpose than the health sweep's per-node queries (`avg by(node)(...)`). Both are needed — the scanner catches fleet-wide trends early, the health sweep identifies specific problematic nodes at peak.

## Tiered Investigation Model

The scanner uses two tiers to balance cost and depth:

```mermaid
flowchart TB
    T1["Tier 1 — Prometheus\n(broad sweep, every 15-30s)"]
    T1_DESC["Fleet-wide PromQL queries\nOne HTTP request covers all nodes\nCatches: node stalls, pending backlogs,\nCPU/memory pressure, Karpenter queue,\nnetwork errors, disk pressure"]

    T1 --> T1_DESC
    T1_DESC --> CHECK{"Finding\ndetected?"}
    CHECK -->|"Yes"| T2
    CHECK -->|"No (or every 4th scan)"| T2_BG["Tier 2 background check"]

    T2["Tier 2 — CloudWatch\n(drill-down, every 60s or on-demand)"]
    T2_DESC["CloudWatch Logs Insights queries\nagainst EKS dataplane logs\nProvides specific error messages\ne.g. Top 10 error patterns"]

    T2 --> T2_DESC
    T2_BG --> T2_DESC
    T2_DESC --> RESULT["ScanResult finding\n→ logged + persisted + included in summary"]

    style T1 fill:#0d3b66,stroke:#1d5a8e,color:#e0e0e0
    style T2 fill:#4a1d6e,stroke:#6b2fa0,color:#e0e0e0
    style RESULT fill:#14532d,stroke:#1a7a3a,color:#e0e0e0
```

Tier 2 only runs when Tier 1 finds something (or every 4th scan cycle as a background check). This avoids unnecessary CloudWatch costs during normal scaling.

## Query Catalog

Queries are defined as data (`ScanQuery` objects) rather than hardcoded logic. Each query specifies which phases it runs in, a condition function, and an evaluator. Adding a new query means appending to the catalog — no changes to the scanner loop.

### Prometheus Queries (Tier 1)

| Query | Phases | Interval | Threshold | What it catches |
|-------|--------|----------|-----------|----------------|
| `node_count` | scaling | 15s | stall detection | Karpenter stopped provisioning |
| `node_not_ready` | scaling, hold | 30s | > 10 nodes | Nodes failing to join cluster |
| `pending_pods` | scaling | 15s | > 60% of target after 5m | Scheduling bottleneck |
| `pod_restarts` | scaling, hold | 30s | > 50 in 5m | Crashloops or OOM kills |
| `cpu_pressure` | scaling, hold | 30s | > 90% fleet avg | CPU saturation |
| `memory_pressure` | hold | 30s | > 80% fleet avg | Memory exhaustion |
| `cpu_outliers` | scaling, hold | 30s | > 5 nodes at 95%+ | CPU hotspots |
| `network_errors` | scaling, hold | 30s | > 10 errors/s | CNI or VPC issues |
| `karpenter_queue_depth` | scaling | 15s | > 1000 | Karpenter backlog |
| `karpenter_errors` | scaling | 30s | > 5 in 2m | EC2 API failures |
| `disk_pressure` | scaling, hold | 30s | > 0 nodes at <10% free | Disk exhaustion |

### CloudWatch Queries (Tier 2)

| Query | Phases | Interval | Trigger | What it provides |
|-------|--------|----------|---------|-----------------|
| `cw_top_errors` | scaling, hold | 60s | Prometheus finding or every 4th scan | Top 10 error patterns in dataplane logs |

## Lifecycle

The scanner follows the controller's test phases:

```mermaid
flowchart TB
    RUN["Controller.run()"] --> P5["Phase 5: Monitoring Setup\nCreate scanner with executor functions\nRegister finding callback\nStart as asyncio.create_task(scanner.run())"]

    P5 --> P7["Phase 7: Scaling\nscanner.set_phase(Phase.SCALING)"]

    P7 --> LOOP["Scaling loop (every 5s)\nscanner.update_context(elapsed, pending, ready)\nScanner runs Tier 1 queries concurrently\nScanner runs Tier 2 if triggered\nFindings → callback → log + evidence store"]

    LOOP --> P7A["Phase 7a: Hold at Peak\nscanner.set_phase(Phase.HOLD_AT_PEAK)\nScanner continues with hold-phase queries"]

    P7A --> STOP["Finally block\nscanner.stop()\nawait scanner_task\nInclude scanner findings in TestRunSummary"]

    style RUN fill:#0d3b66,stroke:#1d5a8e,color:#e0e0e0
    style P7 fill:#7c2d12,stroke:#a3441a,color:#e0e0e0
    style P7A fill:#4a1d6e,stroke:#6b2fa0,color:#e0e0e0
    style STOP fill:#4a3728,stroke:#6b5040,color:#e0e0e0
```

## Context Updates

The controller feeds the scanner live data each polling iteration so that condition functions and evaluators have current state:

| Context Key | Type | Source | Used By |
|-------------|------|--------|---------|
| `elapsed_minutes` | float | Controller scaling loop | `pending_pods` condition (skip first 2m) |
| `pending` | int | `_count_pods()` | `node_count` evaluator (stall detection) |
| `ready` | int | `_count_pods()` | Future evaluators |
| `target_pods` | int | TestConfig | `pending_pods` evaluator (ratio calc) |
| `scan_count` | int | Scanner internal | `cw_top_errors` condition (every 4th scan) |
| `prev_node_count` | float | Scanner internal | `node_count` evaluator (growth tracking) |
| `has_prometheus_finding` | bool | Scanner internal | `cw_top_errors` condition (Tier 2 trigger) |

## Findings

Scanner findings are `ScanResult` dataclass instances with:

| Field | Description |
|-------|-------------|
| `query_name` | Which catalog query produced this (e.g., "cpu_pressure") |
| `severity` | INFO, WARNING, or CRITICAL |
| `title` | One-line summary (e.g., "Fleet avg CPU: 92.3% (threshold: 90)") |
| `detail` | Longer explanation with context |
| `source` | PROMETHEUS or CLOUDWATCH |
| `raw_result` | Raw query response for evidence storage |
| `drill_down_source` | If set, triggers Tier 2 follow-up |

Findings are:
- Logged at WARNING level as they occur
- Persisted to `scanner_findings.jsonl` in the evidence store
- Collected in `self._scanner_findings` (separate from anomaly findings)
- Written to the in-memory SharedContext for cross-source correlation with the anomaly detector
- Included in the `TestRunSummary` under `scanner_findings`

## Cross-Source Correlation

Scanner findings are shared with the anomaly detector via an in-memory `SharedContext` (`shared_context.py`). When the scanner produces a finding, the controller writes it to the SharedContext. When an alert triggers investigation, the anomaly detector queries the SharedContext for temporally relevant scanner findings (within ±120s by default).

If a scanner finding matches the alert's symptoms (same time window, overlapping affected nodes), the anomaly detector references it as prior evidence instead of re-investigating. Matches are classified as:

- **Strong**: Temporal overlap AND resource overlap (scanner found issues on the same nodes)
- **Weak**: Temporal overlap only (scanner found issues but on different or unknown nodes)

This avoids redundant investigation when the scanner already identified the cause. For example, if the scanner detects CPU pressure proactively and a rate drop fires 30 seconds later, the anomaly detector sees the scanner finding and references it rather than re-querying AMP.

## Graceful Degradation

The scanner adapts to what's available:

| Config State | Behavior |
|-------------|----------|
| AMP or Prometheus URL configured | Prometheus queries run, CloudWatch queries run if also configured |
| Only CloudWatch configured | Prometheus queries skipped, CloudWatch queries run |
| Neither configured | Scanner not created at all, test runs normally without it |
| Prometheus query fails at runtime | Error logged, scanner continues with next query |
| CloudWatch query fails at runtime | Error logged, scanner continues |
| Scanner task crashes entirely | Controller logs the failure, test completes without scanner data |

The scanner never blocks or crashes the test run. All query failures are caught and logged at debug level.

## Output Files

| File | Location | Contents |
|------|----------|----------|
| `scanner_findings.jsonl` | Evidence run directory | One JSON line per finding with query_name, severity, title, detail, source, timestamp |

Scanner findings also appear in `summary.json` under the `scanner_findings` key, separate from anomaly detector findings.
