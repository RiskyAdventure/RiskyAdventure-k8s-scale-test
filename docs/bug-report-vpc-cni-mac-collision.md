# Bug Report: VPC CNI FailedCreatePodSandBox Due to Netlink Dump Interruption During Rapid Pod Scaling

## Summary

The VPC CNI plugin fails to create pod sandboxes during rapid pod scaling because `netlink.LinkList()` returns `ErrDumpInterrupted` when concurrent veth creation interrupts the kernel's netlink dump. This causes `FailedCreatePodSandBox` events at a rate of 10-19% of pods on affected nodes, with the error: "failed to generate Unique MAC addr for host side veth: results may be incomplete or inconsistent."

## Environment

- EKS cluster: Wesley beta, `tf-shane` (us-west-2)
- Node AMI: AL2023 (Karpenter-provisioned)
- Kernel: 6.x (AL2023 default)
- VPC CNI version: **v1.20.4** (confirmed via DaemonSet labels and binary inspection on node)
- Kubernetes version: 1.31+

## Reproduction

1. Create a cluster with Karpenter autoscaling
2. Deploy 5,000+ pods simultaneously (e.g., via Deployments with high replica counts)
3. Karpenter provisions ~80-90 new nodes, each receiving 40-110 pods
4. Observe `FailedCreatePodSandBox` events on the majority of nodes

Tested with 5,100 pods across 82 nodes. 557 `FailedCreatePodSandBox` events affecting 507 unique pods (approximately 10% of all pods). 77 of 82 nodes experienced at least one failure.

## Error Message

Every failure produces the identical error (only the sandbox ID varies):

```
Failed to create pod sandbox: rpc error: code = Unknown desc = failed to setup network
for sandbox "<id>": plugin type="aws-cni" name="aws-cni" failed (add): add command:
failed to setup network: SetupPodNetwork: failed to setup veth pair: failed to generate
Unique MAC addr for host side veth: results may be incomplete or inconsistent
```

## Root Cause Analysis

### Code path

The error originates in `cmd/routed-eni-cni-plugin/driver/driver.go` in the `EKSDataPlaneCNIv1` repository:

1. `setupVeth()` (line 389) calls `NewMACGenerator().generateUniqueRandomMAC()` (line 397)
2. `generateUniqueRandomMAC()` (line 715) calls `m.netlink.LinkList()` to enumerate all network interfaces
3. `LinkList()` (pkg/netlinkwrapper/netlink.go:135) calls `netlink.LinkList()` from the vishvananda/netlink library
4. The kernel performs a netlink dump (`RTM_GETLINK` with `NLM_F_DUMP`)
5. If another process creates or deletes a network interface during the dump, the kernel sets `NLM_F_DUMP_INTR` in the response
6. The Go netlink library returns `ErrDumpInterrupted` (error message: "results may be incomplete or inconsistent")
7. `generateUniqueRandomMAC()` wraps this as "failed to generate Unique MAC addr for host side veth"
8. The pod sandbox creation fails

### Why it happens during scaling

During rapid pod scaling, many pods are being set up concurrently on each node. Each pod setup calls `setupVeth()`, which creates a new veth pair. The veth creation (`register_netdevice` in the kernel) modifies the network interface list, which interrupts any in-progress netlink dumps on the same node. When multiple pods are being set up simultaneously, the probability of a netlink dump being interrupted approaches certainty.

### The retry logic gap

VPC CNI v1.20.5 (PR [#3440](https://github.com/aws/amazon-vpc-cni-k8s/pull/3440)) added `retryOnErrDumpInterrupted()` in `pkg/netlinkwrapper/netlink.go:78`, which retries `LinkList()` up to 5 times with 100ms backoff when `ErrDumpInterrupted` is returned.

**Confirmed:** The cluster is running VPC CNI v1.20.4, which does NOT contain the retry logic. This was verified three ways:

1. DaemonSet labels: `app.kubernetes.io/version: v1.20.4`, `helm.sh/chart: aws-vpc-cni-1.20.4`
2. Binary build info on node (via SSM `strings /opt/cni/bin/aws-cni`): `v1.20.4` in ldflags
3. Binary string search: zero occurrences of `retryOnErrDumpInterrupted`, `ErrDumpInterrupted`, or `netlink operation interruption` in the binary

In v1.20.4, a single `NLM_F_DUMP_INTR` flag from the kernel immediately fails the `LinkList()` call with no retry, which propagates up as a fatal error to `generateUniqueRandomMAC()` and kills the pod sandbox creation.

Even with the v1.20.5 retry logic (5 retries, 100ms backoff, 500ms total window), the fix may be insufficient under heavy concurrent load. Our BPF traces show 500-700 netlink dumps per second on affected nodes, with `register_netdevice` bursts of 28-49 calls per second. Under these conditions, every netlink dump attempt within a 500ms window could be interrupted.

## Evidence

### Per-node failure distribution (5K test, 2026-03-19 01:55-01:59 UTC)

| Metric | Value |
|--------|-------|
| Total pods | 5,100 |
| Total nodes | 82 |
| Nodes with failures | 77 (94%) |
| Total FailedCreatePodSandBox events | 557 |
| Unique pods affected | 507 (~10%) |
| Pods with multiple failure events | 48 (retries, max 3 per pod) |

Failure rate by node pod density:

| Pod Density | Nodes | Pods | Failures | Failure Rate |
|-------------|-------|------|----------|-------------|
| High (100+ pods/node) | 7 | 755 | 116 | 15.4% |
| Medium (40-99 pods/node) | 74 | 3,450 | 270 | 7.8% |
| Low (<40 pods/node) | 1 | 39 | 4 | 10.3% |

High-density nodes have approximately 2x the failure rate of medium-density nodes.

### Temporal distribution

All failures occurred in a 4-minute burst:

| Time (UTC) | Failures | Notes |
|------------|----------|-------|
| 01:55 | 410 (74%) | Initial burst as pods schedule onto fresh nodes |
| 01:56 | 86 | Subsiding |
| 01:57 | 32 | |
| 01:58 | 26 | |
| 01:59 | 3 | Nearly done |

All top-failure nodes had the same first-scheduled timestamp (01:55:26), indicating they were all freshly provisioned by Karpenter simultaneously.

### BPF trace data from 3 investigated nodes

30-second bpftrace captures on the 3 highest-failure nodes (collected ~2 min after peak):

| Node | VETH Count | register_netdevice | veth_newlink | netlink_dumps | rtnl_dump_ifinfo |
|------|-----------|-------------------|-------------|---------------|-----------------|
| ip-100-64-66-215 | 77 | 28 | 9 | 21,109 | 305 |
| ip-192-168-172-239 | 87 | 88 | 23 | 20,022 | 527 |
| ip-192-168-187-40 | 110 | 43 | 12 | 16,294 | 475 |

Key observations:
- netlink_dumps are extremely high: 16K-21K in 30 seconds (~500-700/s)
- `register_netdevice` activity is bursty: node 2 had 49 registrations in a single second (t=29)
- `rtnl_dump_ifinfo` (the function that enumerates all interfaces) is called 300-527 times in 30s
- BPF was collected ~2 minutes after the peak, so these numbers represent the tail end of activity; the actual peak was likely higher

### CNI version confirmation (v1.20.4 — no retry logic)

Verified via three independent methods:

1. **DaemonSet labels** (EKS MCP `list_k8s_resources`):
   ```
   app.kubernetes.io/version: v1.20.4
   helm.sh/chart: aws-vpc-cni-1.20.4
   ```

2. **Binary build info** (SSM `strings /opt/cni/bin/aws-cni` on node i-0702539fbf73bd12f):
   ```
   mod  github.com/aws/amazon-vpc-cni-k8s  v1.20.4
   build -ldflags="-s -w -X pkg/version/info.Version=v1.20.4 ..."
   ```

3. **Binary string search** (SSM on same node):
   - `strings /opt/cni/bin/aws-cni | grep -c retryOnErrDumpInterrupted` → **0**
   - `strings /opt/cni/bin/aws-cni | grep -c ErrDumpInterrupted` → **0**
   - `strings /opt/cni/bin/aws-cni | grep -c "netlink operation interruption"` → **0**

The `retryOnErrDumpInterrupted` function (added in v1.20.5, PR #3440) is completely absent from the deployed binary. A single `NLM_F_DUMP_INTR` kernel flag immediately fails the entire pod sandbox creation with no retry.

**Action needed:** Upgrade VPC CNI to v1.20.5+ (latest is v1.21.1).

## Suggested Fixes

### Immediate: Upgrade VPC CNI to v1.20.5+

This adds `retryOnErrDumpInterrupted` with 5 retries and 100ms backoff, which will reduce failures significantly but may not eliminate them entirely under extreme concurrent load.

### Longer-term improvements (upstream suggestions)

1. **Increase retry budget in `retryOnErrDumpInterrupted`**: 5 retries with 100ms delay (500ms total) may be insufficient when `register_netdevice` bursts last multiple seconds. Consider exponential backoff up to 2-3 seconds total.

2. **Add retry at the `generateUniqueRandomMAC` level**: Currently, if `LinkList()` fails after retries, the entire MAC generation fails. A retry around the whole `generateUniqueRandomMAC()` call in `setupVeth()` would provide an additional layer of resilience.

3. **Avoid `LinkList()` entirely for MAC uniqueness**: The MAC generator calls `LinkList()` to build a set of existing MACs, then generates random MACs until one doesn't collide. With 6 bytes of randomness (minus 2 bits for unicast/local), the probability of a random collision is astronomically low (~1 in 70 trillion). The uniqueness check via `LinkList()` is the source of the fragility. Consider removing it or making it best-effort (use the random MAC even if `LinkList()` fails).

4. **Serialize veth creation per node**: If IPAMD serialized veth creation instead of allowing concurrent `setupVeth()` calls, the netlink dump interruption rate would drop dramatically. Trade-off: slower pod startup on nodes with many pending pods.

## Source Code References

All in the `EKSDataPlaneCNIv1` internal repository (mirrors [aws/amazon-vpc-cni-k8s](https://github.com/aws/amazon-vpc-cni-k8s)):

| File | Line | Function |
|------|------|----------|
| `cmd/routed-eni-cni-plugin/driver/driver.go` | 389 | `setupVeth()` — entry point |
| `cmd/routed-eni-cni-plugin/driver/driver.go` | 695-734 | `MACGenerator`, `generateUniqueRandomMAC()` |
| `cmd/routed-eni-cni-plugin/driver/driver.go` | 42 | `MAX_MAC_GENERATION_ATTEMPTS = 10` |
| `pkg/netlinkwrapper/netlink.go` | 78-100 | `retryOnErrDumpInterrupted()` (added v1.20.5) |
| `pkg/netlinkwrapper/netlink.go` | 135-143 | `LinkList()` wrapper |
| vishvananda/netlink `nl/nl_linux.go` | 48-66 | `ErrDumpInterrupted` definition |

## Data Files

- `scale-test-results/2026-03-19_01-38-05/events.jsonl` — 557 FailedCreatePodSandBox events
- `scale-test-results/2026-03-19_01-38-05/diagnostics/` — BPF trace data from 3 nodes
- `scale-test-results/2026-03-19_00-16-39/events.jsonl` — 5,108 FailedCreatePodSandBox events from a separate 30K test
