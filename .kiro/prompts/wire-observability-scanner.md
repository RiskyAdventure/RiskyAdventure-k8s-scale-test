# Wire the ObservabilityScanner into the Controller

## Context

`src/k8s_scale_test/observability.py` contains a new `ObservabilityScanner` module that runs periodic fleet-level Prometheus and CloudWatch queries during scale tests. It is NOT wired into the controller yet. This was intentional — the design needs careful integration to avoid breaking existing modules.

## What the scanner does

- Runs a catalog of PromQL queries (node count, CPU, memory, pending pods, Karpenter queue depth, network errors, disk pressure) on a 15-30s interval during scaling and hold-at-peak phases
- Runs CloudWatch Logs Insights queries (top error patterns) on a 60s interval, triggered when Prometheus findings exist
- Each query has phase tags (scaling vs hold-at-peak), conditions (skip if irrelevant), and evaluator functions
- Produces `ScanResult` findings with severity, title, detail, and optional drill-down source

## Module boundaries — DO NOT VIOLATE

Read each module before making changes. Understand what it owns.

- **monitor.py** owns pod ready rate measurement via K8s deployment watch API. The scanner MUST NOT touch the ticker loop, rate computation, or alert thresholds. These are separate concerns.
- **health_sweep.py** owns per-node health investigation at hold-at-peak. It runs its own per-node PromQL queries (`avg by(node)(...)`) which return per-node results for identifying specific frozen/problematic nodes. The scanner's fleet-aggregated queries (`avg(...)`) serve a different purpose. The health sweep MUST keep its own AMP queries — do not replace them with scanner findings.
- **anomaly.py** owns reactive deep-dive investigation (K8s events, SSM, ENI state). The scanner should feed early warnings INTO the anomaly detector, not replace its investigation pipeline.
- **infra_health.py** owns Karpenter pod resource usage via K8s metrics API. The scanner checks Karpenter scheduling queue and cloud provider errors via Prometheus — different signals, both needed.
- **events.py** owns real-time K8s event streaming. No overlap with the scanner.

## Integration plan

1. **Controller wiring**: In `controller.py`, create the scanner after the monitor starts but before `_execute_scaling_via_flux`. Pass it executor functions that wrap the `AMPMetricCollector._query_promql` method (for Prometheus) and a CloudWatch Logs Insights wrapper (for CloudWatch). Start it as `asyncio.create_task(scanner.run())`. Call `scanner.set_phase(Phase.SCALING)` when stressor scaling begins, `scanner.set_phase(Phase.HOLD_AT_PEAK)` when hold starts. Stop it in the `finally` block alongside the monitor.

2. **Context updates**: In the scaling loop (`_execute_scaling_via_flux`), call `scanner.update_context(elapsed_minutes=..., pending=..., ready=...)` each iteration so the scanner's condition functions have current data.

3. **Finding integration**: Register a callback via `scanner.on_finding()` that logs the finding and appends it to `self._findings`. Scanner findings should be saved to the evidence store as well. Consider a new finding type or prefix to distinguish scanner findings from anomaly detector findings.

4. **Summary inclusion**: Include `scanner.get_findings()` in the test run summary so they appear in the final report.

5. **Graceful degradation**: If AMP is not configured (`amp_workspace_id` is None), the scanner should skip Prometheus queries and only run CloudWatch queries (if configured). If neither is configured, don't start the scanner at all.

## What to test

- Run a 10K pod scale test with the scanner wired in
- Verify scanner findings appear in the log during scaling (not just at the end)
- Verify the health sweep still runs its own per-node AMP queries independently
- Verify the monitor's rate measurement is unaffected (compare rate_data.jsonl data point count and peak rate with a previous run)
- Verify the anomaly detector still fires on rate drops and does its own investigation
- Check that scanner queries don't cause API throttling or credential issues (the AMPMetricCollector creates a new boto3 session — make sure it shares the controller's session or creates its own correctly)

## What NOT to do

- Do not modify monitor.py
- Do not modify health_sweep.py
- Do not modify anomaly.py's investigation pipeline
- Do not add scanner queries to the ticker loop
- Do not replace per-node queries with fleet-aggregated queries anywhere
- Do not hardcode thresholds without making them configurable or at least documented
