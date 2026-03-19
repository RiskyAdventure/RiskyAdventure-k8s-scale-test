# Next Session: FailedCreatePodSandBox + BPF Data Investigation

Read `.kiro/steering/engineering-rigor.md` before starting any work.

## Session Context (March 18, 2026)

### What was done this session

1. **BPF trace root cause found and fixed** — The bpftrace script in `anomaly.py` used `count()` aggregations with `%lld` printf, which bpftrace v0.17.0 (on AL2023 nodes) rejects as a type error. The script failed silently on every invocation because `2>/dev/null` swallowed the error and the merge logic dropped empty-output commands. Fixed by switching to integer variables (`++`), removing the `unregister_netdevice` probe (marked `notrace` on these kernels), and adding `clear()` for all maps in `END` to suppress bpftrace's automatic map printing.
2. **Diagnostics merge logic fixed** — `diagnostics.py` now always includes a section for each extra command even when output is empty, showing status/error. Logs a warning for long-running commands that return empty.
3. **5K validation test run** — `scale-test-results/2026-03-19_01-38-05/`. 5,100 pods, ~92 nodes, 4.4 min scaling. BPF trace data successfully collected on all 3 investigated nodes.
4. **557 FailedCreatePodSandBox events** — 556 of 557 are MAC collision errors ("failed to generate Unique MAC addr for host side veth: results may be incomplete or inconsistent"). Peak at 01:55 UTC (410 events/min).

### Priority 1 Analysis: Per-Node MAC Collision Distribution (COMPLETED)

#### Per-node failure mapping

557 total FailedCreatePodSandBox events → 507 unique pods affected.
427 events mapped to nodes (130 unmapped due to event buffer truncation at 20K events).
48 pods had multiple failure events (retries, max 3 per pod).

**Top 15 nodes by failure count:**

| Rank | Node | Pods Scheduled | Failures | Rate | Notes |
|------|------|---------------|----------|------|-------|
| 1 | ip-192-168-187-40 | 108 | 21 | 19.4% | BPF investigated |
| 2 | ip-192-168-145-218 | 109 | 18 | 16.5% | |
| 3 | ip-192-168-172-239 | 106 | 18 | 17.0% | BPF investigated |
| 4 | ip-192-168-143-79 | 103 | 16 | 15.5% | |
| 5 | ip-100-64-66-215 | 107 | 16 | 15.0% | BPF investigated |
| 6 | ip-100-64-96-229 | 114 | 16 | 14.0% | |
| 7 | ip-100-64-99-124 | 108 | 11 | 10.2% | |
| 8 | ip-192-168-236-70 | 45 | 8 | 17.8% | |
| 9 | ip-192-168-81-148 | 49 | 8 | 16.3% | |
| 10 | ip-192-168-217-38 | 47 | 8 | 17.0% | |

**Key findings:**
- 82 total nodes, 77 had at least one failure, only 5 had zero failures
- The 3 BPF-investigated nodes were ranks 1, 3, 5 — correctly targeted as top offenders
- All top nodes had the same first-scheduled time (01:55:26) — all freshly provisioned by Karpenter

#### Failure rate vs pod density — strong correlation

| Density Group | Nodes | Pods | Failures | Avg Rate |
|--------------|-------|------|----------|----------|
| High (100+ pods) | 7 | 755 | 116 | 15.4% |
| Medium (40-99 pods) | 74 | 3,450 | 270 | 7.8% |
| Low (<40 pods) | 1 | 39 | 4 | 10.3% |

High-density nodes (100+ pods) have ~2x the failure rate of medium-density nodes. This supports the hypothesis that concurrent veth creation on heavily-loaded nodes causes the race condition.

However, the per-node failure rate is remarkably consistent (13-19%) across the top nodes regardless of exact pod count. This suggests the failure rate is driven more by the burst rate of concurrent veth creation than by the absolute number of existing interfaces.

#### Timing analysis

All failures occurred in a 4-minute window:
```
01:55  410 failures (74% of total) — initial burst as pods schedule onto fresh nodes
01:56   86 failures — subsiding
01:57   32 failures — BPF collection starts on node 1
01:58   26 failures — BPF collection starts on nodes 2 and 3
01:59    3 failures — nearly done
```

BPF collection started ~2 minutes after the peak. The trace captured the tail end of activity, not the burst.

### Priority 4 Analysis: VPC CNI Source Code (COMPLETED)

#### MAC generation code path (EKSDataPlaneCNIv1)

The error originates in `cmd/routed-eni-cni-plugin/driver/driver.go`:

```
setupVeth() → line 397
  NewMACGenerator().generateUniqueRandomMAC() → line 715
    m.netlink.LinkList() → gets all interfaces via netlink
    builds macMap of existing MACs
    for 10 attempts:
      generate random MAC
      if not in macMap → return (success)
    return error ("failed to generate unique mac after 10 attempts")
```

The `LinkList()` call goes through `retryOnErrDumpInterrupted` (pkg/netlinkwrapper/netlink.go:78) which retries up to 5 times on `netlink.ErrDumpInterrupted` with 100ms backoff.

#### The race condition — confirmed in source

The error message in our events says "results may be incomplete or inconsistent" — but the actual CNI source says "failed to generate unique mac after 10 attempts." This means the error we're seeing is NOT from `generateUniqueRandomMAC` failing to find a unique MAC. It's from `LinkList()` itself returning `ErrDumpInterrupted` after 5 retries, which propagates up as the error.

Wait — let me re-examine. The error message in the events is:
```
failed to setup veth pair: failed to generate Unique MAC addr for host side veth: results may be incomplete or inconsistent
```

The "results may be incomplete or inconsistent" part is NOT from the CNI code. It's from the netlink library itself — `netlink.ErrDumpInterrupted` has that message. So the actual failure path is:

1. `generateUniqueRandomMAC()` calls `m.netlink.LinkList()`
2. `LinkList()` calls `retryOnErrDumpInterrupted(netlink.LinkList)`
3. `netlink.LinkList()` does a netlink dump of all interfaces
4. The kernel's netlink dump gets interrupted by concurrent interface creation
5. The Go netlink library returns `ErrDumpInterrupted`
6. `retryOnErrDumpInterrupted` retries up to 5 times with 100ms delay
7. If ALL 5 retries fail → error propagates: "netlink operation interruption persisted after 5 attempts: dump interrupted"
8. `generateUniqueRandomMAC` wraps this as "failed to generate Unique MAC addr for host side veth"
9. `setupVeth` wraps as "failed to setup veth pair"

**CRITICAL INSIGHT:** The `retryOnErrDumpInterrupted` function was added in v1.20.5 (PR #3440). If the cluster is running a version BEFORE v1.20.5, there would be NO retry logic — a single `ErrDumpInterrupted` would immediately fail the MAC generation.

Even WITH the retry logic (v1.20.5+), 5 retries with 100ms delay = 500ms total retry window. If the netlink dump is being interrupted continuously (which our BPF data shows — 500-700 dumps/s with concurrent veth creation), 500ms may not be enough.

#### The `MACGenerator` does NOT use `retryOnErrDumpInterrupted`

Actually, re-reading the code more carefully:

```go
// In driver.go
func NewMACGenerator() MACGenerator {
    return MACGenerator{netlink: netlinkwrapper.NewNetLink(), randMACfn: generateRandomMAC}
}
```

`NewNetLink()` returns the wrapper that HAS `retryOnErrDumpInterrupted`. So `LinkList()` in the MAC generator DOES go through the retry logic. Good.

But the MAC generator calls `LinkList()` ONCE, builds the map, then tries 10 random MACs against that map. If `LinkList()` succeeds but returns an incomplete list (which `ErrDumpInterrupted` is supposed to prevent but might not catch all cases), the MAC could collide with an interface that wasn't in the list.

**Alternative failure mode:** The netlink library might return a PARTIAL result without `ErrDumpInterrupted` in some edge cases. The Go vishvananda/netlink library only returns `ErrDumpInterrupted` when it detects the `NLM_F_DUMP_INTR` flag in the netlink response. But the kernel might not always set this flag for all types of interruptions.

### BPF trace data from investigated nodes

| Node | Collection Time | reg_total | vnl_total | netlink_dumps | rtnl_dump_ifinfo | IFACE | VETH |
|------|----------------|-----------|-----------|---------------|-----------------|-------|------|
| ip-100-64-66-215 | 01:57:10 | 28 | 9 | 21,109 | 305 | 80 | 77 |
| ip-192-168-172-239 | 01:57:44 | 88 | 23 | 20,022 | 527 | 90 | 87 |
| ip-192-168-187-40 | 01:58:19 | 43 | 12 | 16,294 | 475 | 113 | 110 |

Per-second activity (only seconds with reg > 0):
```
Node 1 (ip-100-64-66-215):   t=16: reg=28, vnl=9   (single burst)
Node 2 (ip-192-168-172-239): t=28: reg=9, vnl=3 → t=29: reg=49, vnl=5 → t=30: reg=30, vnl=15
Node 3 (ip-192-168-187-40):  t=26: reg=5, vnl=0 → t=27: reg=36, vnl=11 → t=28: reg=2, vnl=1
```

### Hypothesis evaluation

**H1: Netlink dump contention causes MAC collision — SUPPORTED but REFINED**

Original: "high interface count (200+) causes netlink dump interruption."
Data contradicts the 200+ threshold — collisions happen at 77-113 veths.

Revised: The failure is caused by `netlink.LinkList()` returning `ErrDumpInterrupted` during concurrent veth creation, exhausting the 5-retry limit in `retryOnErrDumpInterrupted`. The BPF data shows 500-700 netlink dumps/s happening continuously, and when `register_netdevice` bursts occur (28-49 registrations/s), the dump interruption rate likely spikes.

Negative test: If this were purely about interface count, we'd expect failure rate to correlate with IFACE_COUNT. But the per-node failure rate is ~15% regardless of whether the node has 80 or 113 interfaces. The correlation is with pod density (burst rate), not interface count. This supports the "concurrent creation" variant of H1.

**H2: Race condition during rapid concurrent veth creation — SUPPORTED**

The source code confirms: `generateUniqueRandomMAC()` calls `LinkList()` once, builds a MAC map, then generates random MACs. If two pods are being set up simultaneously on the same node, both call `LinkList()` at nearly the same time, get the same list, and could generate the same "unique" MAC. But this would cause a MAC collision at the kernel level (duplicate MAC on the same host), not the `ErrDumpInterrupted` error we're seeing.

Actually, re-reading the error message: "failed to generate Unique MAC addr for host side veth: results may be incomplete or inconsistent" — the "results may be incomplete or inconsistent" IS the `ErrDumpInterrupted` message. So the failure is `LinkList()` failing after 5 retries, not a MAC collision per se. The function never gets to the MAC generation step because it can't even enumerate existing interfaces.

**H3: BPF collection timing is too late — CONFIRMED**

BPF started 2 min after the 410/min peak. The trace shows the tail end. For a 5K test, the burst is too short (~1 min) for reactive BPF collection to catch it.

### What needs to happen next

#### Priority 1: Determine CNI version on cluster
- Check what VPC CNI version is running on the Wesley beta cluster
- If < v1.20.5, the `retryOnErrDumpInterrupted` fix is not present, and a single `ErrDumpInterrupted` kills the pod
- If >= v1.20.5, the retry logic exists but 5 retries / 500ms may be insufficient under heavy load
- Command: `kubectl describe daemonset aws-node -n kube-system | grep Image`

#### Priority 2: Enhance BPF trace for next run
- Add per-second `netlink_dump` count to the JSON output (currently only `reg` and `vnl` are per-second)
- Consider a proactive BPF mode that starts at scaling begin, not reactively
- The current reactive approach misses the peak by 2+ minutes

#### Priority 3: Run 30K test with fixed BPF
- The 5K test validated the BPF fix works, but data was collected after the peak
- A 30K test has a longer scaling window (~8 min vs ~4 min), giving better chance of capturing peak activity
- Requires: Prometheus remote-write fixed, CL2 namespaces cleaned

#### Priority 4: Consider CNI mitigation options
Based on source code analysis, potential mitigations:
1. **Increase retry count/backoff** in `retryOnErrDumpInterrupted` (currently 5 retries, 100ms delay)
2. **Add retry at the `generateUniqueRandomMAC` level** — if `LinkList()` fails, retry the whole MAC generation
3. **Rate-limit concurrent pod setup** on each node — IPAMD could serialize veth creation
4. **Pre-allocate MACs** during IPAMD warm pool setup instead of at pod creation time
5. **File upstream issue** on aws/amazon-vpc-cni-k8s with this data

#### Priority 5: Remaining items from previous session
- Enable audit logging (user action — Wesley beta cluster)
- Fix Prometheus remote-write (user action)
- Pipeline improvements: CL2 shortfall detection, per-node pod density scanner

### Cluster details
- Wesley beta cluster: `tf-shane`, endpoint `FCC56AB459DA392796365A0DB4F45DE3.sk1.beta.us-west-2.clusters.wesley.amazonaws.com`
- Cluster ID: `547b26ef-79c1-4421-bbb2-39f09970fad2`
- Grafana: `https://g-47c9a49a58.grafana-workspace.us-west-2.amazonaws.com/d/hIUcVOTHz/api-server-troubleshooter-v1-7-1?orgId=1`

### Key files
- `src/k8s_scale_test/anomaly.py` — Fixed bpftrace script (committed)
- `src/k8s_scale_test/diagnostics.py` — Fixed merge logic (committed)
- `scale-test-results/2026-03-19_01-38-05/` — 5K validation run with BPF data
- `scale-test-results/2026-03-19_01-38-05/events.jsonl` — 557 FailedCreatePodSandBox events
- `scale-test-results/2026-03-19_01-38-05/diagnostics/` — 3 node diagnostics with BPF JSON
- `scale-test-results/2026-03-19_00-16-39/` — Previous 30K run (no BPF data, old broken script)
- `.kiro/steering/run-scale-test.md` — Scale test runbook

### VPC CNI source references (EKSDataPlaneCNIv1 repo)
- `cmd/routed-eni-cni-plugin/driver/driver.go:389` — `setupVeth()` entry point
- `cmd/routed-eni-cni-plugin/driver/driver.go:695-734` — `MACGenerator` and `generateUniqueRandomMAC()`
- `pkg/netlinkwrapper/netlink.go:78-100` — `retryOnErrDumpInterrupted()` (added in v1.20.5, PR #3440)
- `pkg/netlinkwrapper/netlink.go:135-143` — `LinkList()` wrapper with retry
- CHANGELOG: v1.20.5 added retry logic; v1.20.0 added MAC assignment to host veth (PR #3354)
