# Architecture Overview

This document explains how the k8s-scale-test application is built, what each piece does, and how they work together.

## What This Application Does

This tool tests how well an EKS (Elastic Kubernetes Service) cluster handles large numbers of pods. It creates thousands of pods on the cluster, measures how fast they start up, detects problems along the way, and produces a report when it's done.

The tool uses a GitOps workflow — instead of talking to Kubernetes directly to create pods, it writes the desired pod counts to a Git repository. A tool called Flux watches that repository and makes the cluster match what's in Git. This is the same workflow used in production, so the test exercises the real deployment path.

## System Diagram

```
┌─────────────────────────────────────────────────────────────────────┐
│                        Scale Test Controller                        │
│                         (controller.py)                             │
│                                                                     │
│  Orchestrates the entire test lifecycle:                            │
│  preflight → infra scaling → stressor scaling → hold → cleanup      │
│                                                                     │
│  ┌──────────┐  ┌──────────┐  ┌───────────┐  ┌──────────────────┐  │
│  │ Preflight│  │   Flux   │  │  Monitor  │  │ Anomaly Detector │  │
│  │ Checker  │  │  Writer  │  │ (rate)    │  │ (investigation)  │  │
│  └────┬─────┘  └────┬─────┘  └─────┬─────┘  └────────┬─────────┘  │
│       │              │              │                  │             │
└───────┼──────────────┼──────────────┼──────────────────┼─────────────┘
        │              │              │                  │
        ▼              ▼              ▼                  ▼
   ┌─────────┐   ┌──────────┐  ┌──────────┐    ┌──────────────┐
   │ AWS APIs│   │ Git Repo │  │ K8s API  │    │  SSM / EC2   │
   │ (EC2,   │   │ (Flux)   │  │ (watch)  │    │  (node logs) │
   │  EKS)   │   │          │  │          │    │              │
   └─────────┘   └──────────┘  └──────────┘    └──────────────┘
```

## Module Map

Every Python file in `src/k8s_scale_test/` has one job. Here's what each one does:

```
┌─────────────────────────────────────────────────────────────────┐
│                    ORCHESTRATION LAYER                           │
│                                                                 │
│  cli.py ──────► controller.py                                   │
│  (parse args)   (run the test)                                  │
│                                                                 │
├─────────────────────────────────────────────────────────────────┤
│                    MEASUREMENT LAYER                             │
│                                                                 │
│  monitor.py          observer (in controller.py)                │
│  (pods/sec via       (pod count via pod list API,               │
│   deployment watch)   independent cross-check)                  │
│                                                                 │
├─────────────────────────────────────────────────────────────────┤
│                    INVESTIGATION LAYER                           │
│                                                                 │
│  anomaly.py          health_sweep.py       infra_health.py      │
│  (reactive deep      (per-node snapshot    (Karpenter pod       │
│   dive on alerts)     at peak load)         health check)       │
│                                                                 │
│  metrics.py          diagnostics.py        events.py            │
│  (node condition     (SSM commands to      (K8s event           │
│   analysis)           collect node logs)    streaming)           │
│                                                                 │
├─────────────────────────────────────────────────────────────────┤
│                    INFRASTRUCTURE LAYER                          │
│                                                                 │
│  flux.py             preflight.py          evidence.py          │
│  (read/write Git     (capacity checks      (save all test       │
│   repo manifests)     before test)          artifacts to disk)  │
│                                                                 │
│  cl2_parser.py       chart.py              agent_context.py     │
│  (parse CL2          (HTML chart from      (context file for    │
│   results)            rate data)            AI sub-agents)      │
│                                                                 │
├─────────────────────────────────────────────────────────────────┤
│                    KNOWLEDGE LAYER                               │
│                                                                 │
│  kb_store.py         kb_matcher.py         kb_seed.py           │
│  (store known        (match findings       (built-in failure    │
│   failure patterns)   against KB)           patterns)           │
│                                                                 │
│  kb_ingest.py        models.py             observability.py     │
│  (auto-add new       (all data             (fleet-level scan    │
│   patterns to KB)     structures)           catalog — proactive  │
│                                             PromQL + CW queries) │
└─────────────────────────────────────────────────────────────────┘
```

## Data Flow During a Test

This diagram shows what data flows where during the scaling phase — the most active part of the test.

```
                    Git Push
  Controller ──────────────────► Flux Repo ──────► Flux ──────► K8s API
       │                                                          │
       │                                                          │
       │  ┌─────────── Deployment Watch ◄─────────────────────────┘
       │  │
       │  ▼
       │  Monitor (ticker loop, 5s)
       │  │
       │  ├──► rate_data.jsonl (evidence store)
       │  │
       │  └──► Alert (rate drop / gap / timeout)
       │            │
       │            ▼
       │       Anomaly Detector
       │       │
       │       ├──► K8s Events (Warning events from cluster)
       │       ├──► Pod Phase Breakdown (Pending vs Running)
       │       ├──► Stuck Pod Nodes (which nodes have problems)
       │       ├──► EC2 ENI State (network interface / IP data)
       │       ├──► SSM Commands (node logs, PSI, disk, CPU)
       │       ├──► KB Lookup (known failure patterns)
       │       │
       │       └──► Finding (saved to evidence store)
       │
       │  ObservabilityScanner (background task, 15-30s)
       │  │
       │  ├──► AMP PromQL (fleet-wide CPU, memory, pending, Karpenter)
       │  ├──► CloudWatch Logs Insights (error patterns, on-demand)
       │  │
       │  └──► scanner_findings.jsonl (evidence store)
       │
       │  Observer (pod list API, 10s)
       │  │
       │  └──► observer.log (independent cross-check)
       │
       │  Event Watcher (K8s watch API)
       │  │
       │  └──► events.jsonl (all Warning events)
```

## Key Design Decisions

**Why GitOps instead of direct kubectl?**
The test exercises the real deployment path. In production, changes go through Git → Flux → K8s. Testing this path catches Flux reconciliation delays, Git push failures, and kustomization issues that a direct `kubectl scale` would miss.

**Why two pod counting methods (monitor + observer)?**
The monitor uses the Deployment watch API (`.status.readyReplicas`). The observer uses the Pod list API (actual Running pods). These are different K8s API endpoints with different failure modes. If the deployment controller's bookkeeping drifts from reality, the observer catches it. If the observer's pod list call fails, the monitor still works.

**Why reactive investigation instead of continuous monitoring?**
The anomaly detector only runs when something goes wrong (rate drop, timeout, monitor gap). This is intentional — at 30K pods, the K8s API is under heavy load. Running continuous SSM commands and EC2 API calls would add more pressure. The anomaly detector fires targeted queries only when needed. The ObservabilityScanner complements this with lightweight fleet-wide PromQL queries (one HTTP request covers all nodes) that run every 15-30s — cheap enough to run continuously without adding API pressure. CloudWatch drill-downs only trigger when Prometheus finds something.

**Why a Known Issues KB?**
Many scale test failures are the same patterns repeating (IPAMD MAC collision, ECR pull throttle, NVMe disk init). The KB short-circuits investigation — if the K8s events match a known pattern, the anomaly detector returns the known root cause immediately instead of running SSM commands on 3 nodes.
