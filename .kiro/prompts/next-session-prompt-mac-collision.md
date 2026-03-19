# Next Session: VPC CNI MAC Collision + CL2 Shortfall Investigation

Read `.kiro/steering/engineering-rigor.md` before starting any work.

## Session Context (March 18, 2026)

### What was done this session

1. **SSM timeout fix** — `diagnostics.py` now gives `bpf_netlink_trace` a 60s SSM timeout (was 30s, bpftrace runs for 30s + overhead). Test passes.
2. **Enforce-rigor hook** — Changed from `preToolUse` on `write` (hung every edit) to `promptSubmit` (fires once per prompt). Re-enabled.
3. **Run-scale-test runbook** — Updated to use `nohup` + `executeBash` instead of `controlBashProcess` (which rejects the command).
4. **30K scale test run** — `scale-test-results/2026-03-19_00-16-39/`. 30,600 pods, ~240 nodes, 8.6 min scaling.
5. **CL2 standalone isolation test** — Ran CL2 alone on idle cluster. Created 100% of objects (29,250 ConfigMaps, 30,000 Secrets). Proves shortfall is caused by API server contention during concurrent pod scaling, not a CL2 bug.

### Key findings

**CL2 shortfall root cause confirmed:**
- Standalone CL2 creates all objects successfully (29,250 CM + 30,000 Secrets)
- During full 30K test: only 21,982 CM (75%) and 23,718 Secrets (79%)
- CL2's Go client has a default rate limiter (~5 QPS burst 10) causing client-side throttling
- AMP showed 504s and 429s on configmaps POST during the full test, but Prometheus remote-write was down so this data may be unreliable
- The combined load (CL2 + 30K pods + Flux + Karpenter + controllers) overwhelms the API server, CL2 steps timeout before completing

**MAC collisions — worse than expected:**
- 5,108 MAC collision events (up from 14 last run), 4,303 unique pods affected
- Peak at 00:19-00:21 UTC (~1,400 events/min)
- BUT investigated nodes had LOW veth counts: 38, 70, 90 — well below the 100+ threshold
- This contradicts the "high interface count" hypothesis. Needs re-investigation.

**BPF trace — still missing:**
- All 3 investigated nodes show no BPF trace output despite the 60s timeout fix
- The merge logic in diagnostics.py only includes extra commands with non-empty output
- Need to verify: is bpftrace installed on these nodes? Did the EC2NodeClass userData propagate?

**Prometheus/AMP — unreliable:**
- The Prometheus instance remote-writing to AMP was down during this session
- AMP query results from this run should not be trusted
- User is fixing this

### Cluster details
- Wesley beta cluster: `tf-shane`, endpoint `FCC56AB459DA392796365A0DB4F45DE3.sk1.beta.us-west-2.clusters.wesley.amazonaws.com`
- Cluster ID: `547b26ef-79c1-4421-bbb2-39f09970fad2`
- No control plane audit logging enabled (standard `aws eks` API doesn't work for Wesley clusters)
- Grafana: `https://g-47c9a49a58.grafana-workspace.us-west-2.amazonaws.com/d/hIUcVOTHz/api-server-troubleshooter-v1-7-1?orgId=1`

## What needs to happen next

### Priority 1: Enable audit logging
- This is a Wesley beta cluster — standard `aws eks update-cluster-config` doesn't work
- User will enable in the morning
- Once enabled, query audit logs to correlate 504s/429s with specific CL2 namespace POST calls vs other ConfigMap creators
- Log group name should be: `master/547b26ef-79c1-4421-bbb2-39f09970fad2`

### Priority 2: Fix Prometheus remote-write
- User is working on this
- Once fixed, re-run 30K test to get reliable AMP data
- Key metrics to watch: `apiserver_request_total{code=~"429|504"}`, `apiserver_flowcontrol_rejected_requests_total`, `apiserver_flowcontrol_current_inqueue_requests`

### Priority 3: Investigate MAC collision regression
- 14 → 5,108 events between runs. Why?
- Investigated nodes had low veth counts (38, 70, 90). The "netlink dump interruption at high interface count" hypothesis doesn't explain this.
- Hypotheses to test:
  - Diagnostics ran too late — pods already cleaned up, veth counts dropped by collection time
  - Collision happens during rapid concurrent veth creation (race condition) not from large existing pool
  - Something changed between runs (different node pool, CNI version, timing)
- Need per-node pod counts at the TIME of the collisions (requires working AMP)
- Check CloudWatch dataplane logs for IPAMD errors correlated with the collision timestamps

### Priority 4: Fix BPF trace collection
- Verify bpftrace is installed on Karpenter-provisioned nodes (check EC2NodeClass userData)
- The SSM command returns empty output — is it a permissions issue? bpftrace needs root/CAP_BPF
- Consider adding a fallback: if bpf output is empty, log a warning with the SSM status

### Priority 5: Pipeline improvements
- Add CL2 shortfall detection to controller (compare actual vs planned, warn if >5% shortfall)
- Add per-node pod density scanner query to observability.py
- Scanner only produced `cw_top_errors` info findings — no proactive MAC collision detection
- No CL2 object monitoring at all in scanner or anomaly detector

### Priority 6: Pipeline evaluation checklist
Fill in from the session prompt's checklist using data from `scale-test-results/2026-03-19_00-16-39/`

## Key files
- `src/k8s_scale_test/diagnostics.py` — SSM timeout fix (done)
- `src/k8s_scale_test/anomaly.py` — bpf command already added
- `src/k8s_scale_test/controller.py` — CL2 shortfall detection needed
- `src/k8s_scale_test/observability.py` — per-node pod density query needed
- `scale-test-results/2026-03-19_00-16-39/` — latest 30K run
- `scale-test-results/2026-03-19_00-16-39/events.jsonl` — 5,108 MAC collision events
- `scale-test-results/2026-03-19_00-16-39/findings/finding-3b45587e.json` — anomaly finding
- `scale-test-results/2026-03-19_00-16-39/diagnostics/` — SSM diagnostics (no BPF data)
- `.kiro/steering/run-scale-test.md` — updated runbook (nohup approach)
- `.kiro/hooks/enforce-rigor.kiro.hook` — re-enabled with promptSubmit
