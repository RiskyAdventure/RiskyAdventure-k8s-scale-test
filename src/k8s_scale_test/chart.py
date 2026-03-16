"""Generate an HTML chart from scale test rate data.

Produces a self-contained HTML file with an interactive chart showing:
- Pod ready rate (pods/sec) over time
- Cumulative ready count over time
- Pending count over time
- Scaling step markers
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)


def generate_chart(run_dir: str, steps: list[dict] | None = None) -> str:
    """Generate chart HTML from rate_data.jsonl. Returns path to HTML file."""
    rd = Path(run_dir)
    rate_file = rd / "rate_data.jsonl"
    if not rate_file.exists():
        log.warning("No rate_data.jsonl found in %s", run_dir)
        return ""

    points = []
    for line in rate_file.read_text().splitlines():
        if line.strip():
            points.append(json.loads(line))
    if not points:
        return ""

    t0 = points[0]["timestamp"]
    timestamps, ready_rates, rolling_avgs = [], [], []
    ready_counts, pending_counts, total_pods = [], [], []
    gap_flags = []

    # Find the first point where pending > 0 (K8s starts working)
    first_pending_ts = None
    for p in points:
        if p.get("pending_count", 0) > 0:
            first_pending_ts = p["timestamp"]
            break

    # Use first pending as chart origin — excludes Flux reconcile time
    chart_origin = first_pending_ts or t0
    provisioning_seconds = 0.0
    first_ready_ts = None
    for p in points:
        if p.get("ready_count", 0) > 0:
            first_ready_ts = p["timestamp"]
            break
    if first_ready_ts and first_pending_ts:
        from datetime import datetime as _dt
        _dt_pending = _dt.fromisoformat(first_pending_ts) if isinstance(first_pending_ts, str) else first_pending_ts
        _dt_ready = _dt.fromisoformat(first_ready_ts) if isinstance(first_ready_ts, str) else first_ready_ts
        provisioning_seconds = (_dt_ready - _dt_pending).total_seconds()

    for p in points:
        ts = p["timestamp"]
        if isinstance(ts, str):
            from datetime import datetime
            dt = datetime.fromisoformat(ts)
            dt_origin = datetime.fromisoformat(chart_origin) if isinstance(chart_origin, str) else chart_origin
            dt0 = datetime.fromisoformat(t0) if isinstance(t0, str) else t0
            sec = (dt - dt_origin).total_seconds()
            if first_ready_ts and provisioning_seconds == 0.0:
                provisioning_seconds = (dt_origin - dt0).total_seconds()
        else:
            sec = 0
        # Compute seconds from test start
        if sec < 0:
            sec = 0
        timestamps.append(round(sec, 1))
        is_gap = p.get("is_gap", False)
        gap_flags.append(is_gap)
        ready_rates.append(round(p.get("ready_rate", 0), 2))
        rolling_avgs.append(round(p.get("rolling_avg_rate", 0), 2))
        ready_counts.append(p.get("ready_count", 0))
        pending_counts.append(p.get("pending_count", 0))
        total_pods.append(p.get("total_pods", 0))

    # Y max for rate chart: p95 of non-zero, non-gap rates to avoid distortion
    clean_rates = sorted([r for r, g in zip(ready_rates, gap_flags) if r > 0 and not g])
    if clean_rates:
        p95_rate = clean_rates[int(len(clean_rates) * 0.95)]
        rate_y_max = round(p95_rate * 1.5, -1)
        rate_y_max = max(rate_y_max, 10)
    else:
        rate_y_max = None

    peak_ready = max(ready_counts)
    peak_rate = max(clean_rates) if clean_rates else max(ready_rates) if ready_rates else 0
    peak_pending = max(pending_counts)
    duration_s = timestamps[-1] if timestamps else 0
    gap_count = sum(gap_flags)

    # Load summary if available
    summary_data = {}
    summary_file = rd / "summary.json"
    if summary_file.exists():
        try:
            summary_data = json.loads(summary_file.read_text())
        except Exception:
            pass

    # Build summary table rows from steps and summary
    sr = summary_data.get("scaling_result", {})
    validity = summary_data.get("validity", "unknown")
    validity_reason = summary_data.get("validity_reason", "")
    run_id = summary_data.get("run_id", rd.name)
    target = summary_data.get("target_pods", sr.get("total_pods_requested", peak_ready))
    nodes = sr.get("total_nodes_provisioned", 0)
    findings_count = len(summary_data.get("findings", []))

    # Compute stats from actual rate data (source of truth)
    first_pending_point = next((p for p in points if p.get("pending_count", 0) > 0), None)
    # Find scaling completion: highest ready count before the delete phase starts
    peak_idx = max(range(len(points)), key=lambda i: points[i].get("ready_count", 0))
    done_point = points[peak_idx] if points[peak_idx].get("ready_count", 0) >= target * 0.99 else None
    scaling_duration_s = 0.0
    avg_rate_clean = 0.0
    if first_pending_point and done_point:
        from datetime import datetime as _dt
        _fp = _dt.fromisoformat(first_pending_point["timestamp"])
        _dn = _dt.fromisoformat(done_point["timestamp"])
        scaling_duration_s = (_dn - _fp).total_seconds()
        if scaling_duration_s > 0:
            avg_rate_clean = target / scaling_duration_s

    # Build the raw data table
    data_rows = ""
    from datetime import datetime as _dtc
    _t0c = _dtc.fromisoformat(points[0]["timestamp"]) if points else None
    for p in points:
        _dtx = _dtc.fromisoformat(p["timestamp"])
        elapsed = (_dtx - _t0c).total_seconds() if _t0c else 0
        ts_short = p["timestamp"][11:19]
        rc = p.get("ready_count", 0)
        pc = p.get("pending_count", 0)
        tp = p.get("total_pods", 0)
        rate = p.get("ready_rate", 0)
        iv = p.get("interval_seconds", 0)
        gap = "⚠" if p.get("is_gap") else ""
        data_rows += (
            f"<tr><td>{elapsed:.0f}s</td><td>{ts_short}</td>"
            f"<td>{rc:,}</td><td>{pc:,}</td><td>{tp:,}</td>"
            f"<td>{rate:.1f}</td>"
            f"<td>{iv:.1f}s</td><td>{gap}</td></tr>\n"
        )

    # Derive pass/fail from rate data directly
    reached_target = peak_ready >= target if target > 0 else False
    result_class = "pass" if reached_target else "fail"
    result_label = "PASSED" if reached_target else "FAILED"
    validity_display = "success" if reached_target else f"reached {peak_ready:,}/{target:,}"

    # Load warning events — filter out expected transient noise
    events_file = rd / "events.jsonl"
    TRANSIENT_REASONS = {"FailedScheduling", "InvalidDiskCapacity"}  # expected during provisioning
    warning_reasons: dict[str, dict] = {}
    transient_count = 0
    if events_file.exists():
        try:
            for line in events_file.read_text().splitlines():
                if not line.strip():
                    continue
                ev = json.loads(line)
                if ev.get("event_type") != "Warning":
                    continue
                reason = ev.get("reason", "Unknown")
                if reason in TRANSIENT_REASONS:
                    transient_count += 1
                    continue
                if reason not in warning_reasons:
                    warning_reasons[reason] = {
                        "count": 0,
                        "message": ev.get("message", "")[:200],
                        "kind": ev.get("involved_object_kind", ""),
                    }
                warning_reasons[reason]["count"] += 1
        except Exception:
            pass

    error_rows = ""
    for reason, info in sorted(warning_reasons.items(), key=lambda x: -x[1]["count"]):
        error_rows += (
            f"<tr><td style='color:#e94560'>{reason}</td>"
            f"<td>{info['count']:,}</td>"
            f"<td>{info['kind']}</td>"
            f"<td style='color:#999;font-size:12px'>{info['message'][:150]}</td></tr>\n"
        )

    error_table = ""
    if error_rows:
        transient_note = f"<div class='note'>{transient_count:,} FailedScheduling events filtered (expected during node provisioning)</div>" if transient_count else ""
        error_table = f"""<div class="chart-box">
<table>
<tr><th style="text-align:left">Error</th><th>Count</th><th>Object</th><th style="text-align:left">Sample Message</th></tr>
{error_rows}
</table>
{transient_note}
</div>"""

    # --- CL2 preload section (plan + actual object counts only) ---
    cl2_html = ""
    cl2_summary_file = rd / "cl2_summary.json"
    if cl2_summary_file.exists():
        try:
            cl2_data = json.loads(cl2_summary_file.read_text())
            cl2_status = cl2_data.get("test_status", {})
            cl2_config = cl2_status.get("config_name", "unknown")
            cl2_result = cl2_status.get("status", "unknown")
            cl2_duration = cl2_status.get("duration_seconds", 0)
            cl2_error = cl2_status.get("error_message")

            error_line = ""
            if cl2_result in ("Failed", "Timeout") and cl2_error and "No PerfData" not in str(cl2_error):
                error_line = f"<tr><td style='color:#e94560'>Error</td><td colspan='2' style='color:#e94560'>{cl2_error}</td></tr>"

            # Compute per-object-type creation rates
            rate_line = ""
            plan = cl2_data.get("preload_plan")
            if plan and cl2_duration > 0:
                rate_rows = ""
                for label, key in [("Deployments", "actual_deployments"), ("Services", "actual_services"),
                                   ("ConfigMaps", "actual_configmaps"), ("Secrets", "actual_secrets")]:
                    count = plan.get(key, 0)
                    if count > 0:
                        r = count / cl2_duration
                        rate_rows += f"<tr><td style='color:#aaa'>{label}</td><td colspan='2'>{r:.0f}/s ({count:,} in {cl2_duration:.0f}s)</td></tr>\n"
                if rate_rows:
                    rate_line = f"<tr><td colspan='3' style='color:#aaa;padding-top:8px;font-weight:bold'>Creation Rates</td></tr>\n{rate_rows}"
            plan_rows = ""
            if plan:
                actual = plan.get('actual_total', 0)
                planned = plan.get('total_objects', 0)
                match_class = "color:#4ecca3" if actual >= planned * 0.95 else "color:#e94560"

                def _row(label, actual_key, planned_key):
                    a = plan.get(actual_key, 0)
                    p = plan.get(planned_key, 0)
                    if a == 0 and p == 0:
                        return ""
                    c = "color:#4ecca3" if a >= p else "color:#e94560"
                    return f'<tr><td style="color:#aaa">{label}</td><td>{p:,}</td><td style="{c}">{a:,}</td></tr>\n'

                plan_rows = f"""<tr><th style="text-align:left;color:#aaa">Object Type</th><th style="color:#aaa">Planned</th><th style="color:#aaa">Actual</th></tr>
{_row("Namespaces", "actual_namespaces", "namespaces")}
{_row("Deployments", "actual_deployments", "total_deployments")}
{_row("Pods", "actual_pods", "total_pods")}
{_row("Services", "actual_services", "total_services")}
{_row("ConfigMaps", "actual_configmaps", "total_configmaps")}
{_row("Secrets", "actual_secrets", "total_secrets")}
<tr style="border-top:2px solid #2a2a4a"><td style="color:#aaa;font-weight:bold">Total</td><td style="font-weight:bold">{planned:,}</td><td style="font-weight:bold;{match_class}">{actual:,}</td></tr>"""

            # If CL2 completed but we just couldn't parse metrics, show as completed
            display_status = cl2_result
            if cl2_result == "Failed" and cl2_error and "Parse error" in str(cl2_error):
                display_status = "Completed (no metrics)"

            cl2_html = f"""<div class="chart-box">
<h2 style="color:#e94560;margin-top:0">CL2 Preload</h2>
<table>
<tr><td style="color:#aaa">Config</td><td colspan="2">{cl2_config}</td></tr>
<tr><td style="color:#aaa">Status</td><td colspan="2">{display_status}</td></tr>
<tr><td style="color:#aaa">Duration</td><td colspan="2">{cl2_duration:.0f}s</td></tr>
{rate_line}
{error_line}
{plan_rows}
</table>
</div>
"""
        except Exception as exc:
            log.warning("Failed to load cl2_summary.json: %s", exc)

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>Scale Test — {run_id}</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
body {{ font-family: -apple-system, sans-serif; margin: 20px; background: #1a1a2e; color: #eee; }}
.chart-box {{ background: #16213e; border-radius: 8px; padding: 20px; margin-bottom: 20px; }}
h1 {{ color: #e94560; margin-bottom: 4px; }}
.run-id {{ color: #888; font-size: 13px; margin-bottom: 16px; }}
.stats {{ display: flex; gap: 16px; margin-bottom: 20px; flex-wrap: wrap; }}
.stat {{ background: #0f3460; padding: 12px 20px; border-radius: 8px; }}
.stat .val {{ font-size: 24px; font-weight: bold; color: #e94560; }}
.stat .lbl {{ font-size: 11px; color: #aaa; }}
table {{ border-collapse: collapse; width: 100%; margin-bottom: 20px; }}
th, td {{ padding: 8px 14px; text-align: right; border-bottom: 1px solid #2a2a4a; }}
th {{ color: #aaa; font-size: 12px; text-transform: uppercase; }}
td {{ color: #ddd; font-size: 14px; }}
td:first-child, th:first-child {{ text-align: left; }}
.result {{ font-size: 18px; font-weight: bold; padding: 4px 12px; border-radius: 4px; display: inline-block; margin-bottom: 12px; }}
.result.pass {{ background: #1b4332; color: #4ecca3; }}
.result.fail {{ background: #4a1525; color: #e94560; }}
.note {{ color: #666; font-size: 11px; margin-top: 4px; }}
.data-table {{ max-height: 400px; overflow-y: auto; }}
.data-table table {{ font-size: 12px; }}
.data-table td, .data-table th {{ padding: 4px 10px; white-space: nowrap; }}
</style></head><body>
<h1>Scale Test Results</h1>
<div class="run-id">{run_id}</div>
<div class="result {result_class}">{result_label} — {validity_display}</div>

<div class="chart-box">
<table>
<tr><th style="text-align:left">Metric</th><th>Value</th></tr>
<tr><td style="color:#aaa">Target</td><td>{target:,} pods</td></tr>
<tr><td style="color:#aaa">Reached</td><td>{peak_ready:,} pods</td></tr>
<tr><td style="color:#aaa">Scaling Duration</td><td>{scaling_duration_s/60:.1f}m ({scaling_duration_s:.0f}s)</td></tr>
<tr><td style="color:#aaa">Avg Rate</td><td>{avg_rate_clean:.1f} pods/s</td></tr>
<tr><td style="color:#aaa">Peak Rate (clean)</td><td>{peak_rate:.1f} pods/s</td></tr>
<tr><td style="color:#aaa">Nodes</td><td>{nodes}</td></tr>
<tr><td style="color:#aaa">Anomalies</td><td>{findings_count}</td></tr>
<tr><td style="color:#aaa">Data Points</td><td>{len(points)} ({gap_count} gaps)</td></tr>
</table>
</div>

{f'<div style="color:#888;font-size:13px;margin-bottom:16px">Node provisioning: {provisioning_seconds:.0f}s before first pod ready.</div>' if provisioning_seconds > 10 else ''}

{cl2_html}

<div class="chart-box data-table">
<table>
<tr><th>Elapsed</th><th>Time</th><th>Ready</th><th>Pending</th><th>Total</th><th>Rate/s</th><th>Interval</th><th>Gap</th></tr>
{data_rows}
</table>
</div>

<div class="chart-box">
<canvas id="rateChart" height="100"></canvas>
<div class="note">Y axis capped at p95 × 1.5 to avoid spike distortion</div>
</div>
<div class="chart-box">
<canvas id="countChart" height="100"></canvas>
</div>

<script>
const ts = {json.dumps(timestamps)};
const gaps = {json.dumps(gap_flags)};
const rates = {json.dumps(ready_rates)};
const rolling = {json.dumps(rolling_avgs)};
const ready = {json.dumps(ready_counts)};
const pending = {json.dumps(pending_counts)};
const total = {json.dumps(total_pods)};
const rateData = ts.map((t, i) => ({{x: t, y: rates[i]}}));
const readyData = ts.map((t, i) => ({{x: t, y: ready[i]}}));
const pendingData = ts.map((t, i) => ({{x: t, y: pending[i]}}));
const totalData = ts.map((t, i) => ({{x: t, y: total[i]}}));
const rateColors = gaps.map(g => g ? 'rgba(255,165,0,0.6)' : '#e94560');
const rateRadii = gaps.map(g => g ? 5 : 2);
const rateYMax = {json.dumps(rate_y_max)};
"""

    html += """
new Chart(document.getElementById('rateChart'), {
  type: 'scatter',
  data: {
    datasets: [
      { label: 'Ready Rate (pods/sec)', data: rateData, borderColor: '#e94560',
        borderWidth: 2, pointBackgroundColor: rateColors, pointRadius: rateRadii,
        showLine: true, fill: false, tension: 0.1 },

    ]
  },
  options: {
    responsive: true,
    plugins: {
      title: { display: true, text: 'Pod Ready Rate Over Time', color: '#eee' },
      legend: { labels: { color: '#ccc' } },
      tooltip: {
        callbacks: {
          afterLabel: function(ctx) {
            if (gaps[ctx.dataIndex]) return '⚠ GAP: averaged over ' + (ts[ctx.dataIndex] - (ctx.dataIndex > 0 ? ts[ctx.dataIndex-1] : 0)).toFixed(0) + 's';
            return '';
          }
        }
      }
    },
    scales: {
      x: { type: 'linear', title: { display: true, text: 'Seconds', color: '#aaa' },
           ticks: { color: '#888', stepSize: 60 }, min: 0 },
      y: { title: { display: true, text: 'Pods/sec', color: '#aaa' }, ticks: { color: '#888' },
           min: 0, ...(rateYMax ? { max: rateYMax } : {}) }
    }
  }
});

new Chart(document.getElementById('countChart'), {
  type: 'scatter',
  data: {
    datasets: [
      { label: 'Ready', data: readyData, borderColor: '#4ecca3', borderWidth: 2,
        pointRadius: 1, showLine: true, fill: true, backgroundColor: 'rgba(78,204,163,0.15)', tension: 0.1 },
      { label: 'Pending', data: pendingData, borderColor: '#e94560', borderWidth: 2,
        pointRadius: 1, showLine: true, fill: true, backgroundColor: 'rgba(233,69,96,0.15)', tension: 0.1 },
      { label: 'Total', data: totalData, borderColor: '#888', borderWidth: 1,
        pointRadius: 0, showLine: true, borderDash: [3,3], fill: false },
    ]
  },
  options: {
    responsive: true,
    plugins: {
      title: { display: true, text: 'Pod Counts Over Time', color: '#eee' },
      legend: { labels: { color: '#ccc' } }
    },
    scales: {
      x: { type: 'linear', title: { display: true, text: 'Seconds', color: '#aaa' },
           ticks: { color: '#888', stepSize: 60 }, min: 0 },
      y: { title: { display: true, text: 'Pods', color: '#aaa' }, ticks: { color: '#888' }, min: 0 }
    }
  }
});
</script>
""" + error_table + """

</body></html>"""

    out_path = rd / "chart.html"
    out_path.write_text(html)
    log.info("Chart written to %s", out_path)
    return str(out_path)
