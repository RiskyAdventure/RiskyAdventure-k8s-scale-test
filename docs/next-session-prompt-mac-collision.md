# Next Session: Deep Investigation — VPC CNI MAC Address Collision at Scale

Read `.kiro/steering/engineering-rigor.md` before starting any work.

## Problem Statement

During 30K pod scale tests on EKS, pods fail with `FailedCreatePodSandBox` and the error:

```
failed to setup network for sandbox: plugin type="aws-cni" name="aws-cni" failed (add):
add command: failed to setup veth pair: failed to generate Unique MAC addr for host side veth:
results may be incomplete or inconsistent
```

This happens at high pod density (>100 pods/node on i4i.8xlarge instances with 140 usable pods/node). The issue causes a long tail in scaling — the last ~100-200 pods take 3-5 extra minutes to become ready because they keep hitting MAC collisions and retrying.

## What We Know So Far

- **Cluster**: EKS `tf-shane`, us-west-2, Karpenter-managed i4i.8xlarge nodes
- **VPC CNI version**: v1.20.4-eksbuild.2 (current as of March 2026)
- **Pod density**: 140 pods/node (150 maxPods - 10 daemonsets)
- **Instance type**: i4i.8xlarge (8 ENIs × 30 IPs/ENI with prefix delegation)
- **Prefix delegation**: Enabled (ENABLE_PREFIX_DELEGATION=true)
- **Observed at**: ~24,000-30,000 pods across ~215 nodes
- **Impact**: Last 100-200 pods take 3-5 extra minutes; pods eventually recover after retries
- **KB entry**: `ipamd-mac-collision` claims fixed in VPC CNI v1.12.4 — this is WRONG, we see it at v1.20.4

## Investigation Goals

### 1. Understand the MAC Generation Algorithm

- Read the VPC CNI source code (https://github.com/aws/amazon-vpc-cni-k8s) to understand how veth MAC addresses are generated
- Identify the exact function that generates MACs and the collision detection logic
- Determine: is this a birthday paradox problem (random MACs in a finite space), or a bug in the generation algorithm?
- What is the MAC address space being used? (locally administered MACs = 2^46 addresses, but the CNI may use a smaller space)
- How many retries does the CNI attempt before failing?

### 2. Determine the Exact Threshold

- At what pod density does the collision probability become significant?
- Model this mathematically: given N existing veth pairs on a node, what's the probability of collision on the (N+1)th?
- Cross-reference with our test data: at what pod count per node did we start seeing FailedCreatePodSandBox?
- Check the IPAMD logs from our test runs for the exact collision counts per node:
  - `scale-test-results/2026-03-18_14-42-47/diagnostics/` — SSM outputs from affected nodes
  - `scale-test-results/2026-03-18_14-42-47/events.jsonl` — K8s events with FailedCreatePodSandBox counts

### 3. Scan for Open Issues and PRs

- Search the VPC CNI GitHub repo for open issues related to MAC collision:
  - https://github.com/aws/amazon-vpc-cni-k8s/issues
  - Search terms: "MAC collision", "unique MAC", "veth pair", "failed to generate"
- Check if there are any open PRs addressing this
- Check the VPC CNI changelog for any recent fixes related to MAC generation
- Look at EKS release notes for any mentions of this issue
- Search AWS re:Post and Stack Overflow for community reports

### 4. Evaluate Potential Solutions

For each solution, assess: effectiveness, complexity, risk, and whether it requires a CNI change or is a workaround.

**A. CNI-level fixes (require VPC CNI changes)**
- Deterministic MAC generation (hash-based instead of random) — would this eliminate collisions?
- Larger retry count with exponential backoff
- Use the pod's IP address as part of the MAC (guaranteed unique per node)
- Pre-allocate MACs from a pool instead of generating on-demand

**B. Cluster-level workarounds (no CNI changes needed)**
- Reduce maxPods per node (e.g., 110 instead of 150) — at what threshold does the problem disappear?
- Use larger instance types with more ENIs (spreads pods across more network interfaces)
- Increase Karpenter node count to reduce per-node density
- Custom init container that pre-creates veth pairs

**C. Operational mitigations**
- Retry logic in the scale test controller (detect MAC collision, wait, retry)
- Exclude affected nodes from further scheduling temporarily
- Monitor per-node pod density and alert before hitting the collision threshold

### 5. Verify and Update the KB Entry

The current KB entry (`ipamd-mac-collision` in `src/k8s_scale_test/kb_seed.py`) has issues:

1. **`fixed_in: "1.12.4"` is wrong** — we see the issue at v1.20.4. Either the fix was incomplete, or the issue was reintroduced, or the original fix addressed a different variant.
2. **Root cause description** says "exhausts the MAC address space" — verify if this is accurate or if it's a collision probability issue (birthday paradox) rather than actual exhaustion.
3. **Recommended actions** suggest upgrading to v1.12.4+ — this doesn't help since we're already past that version.
4. **Missing**: threshold information (at what density does this start?), retry behavior, and whether prefix delegation affects the collision rate.

Update the KB entry with accurate information based on the investigation findings.

### 6. Quantify the Impact

Using data from our test runs:
- How many total FailedCreatePodSandBox events occurred?
- What percentage of pods were affected?
- What was the average retry time before success?
- Did any pods permanently fail (never recovered)?
- What's the cost in scaling time? (e.g., "adds 3-5 minutes to a 30K pod scale-up")

## Key Files

- `src/k8s_scale_test/kb_seed.py` — KB entry to update
- `scale-test-results/2026-03-18_14-42-47/` — latest test run with MAC collisions
- `scale-test-results/2026-03-18_14-42-47/events.jsonl` — K8s events
- `scale-test-results/2026-03-18_14-42-47/findings/` — anomaly detector findings
- `scale-test-results/2026-03-18_14-42-47/diagnostics/` — SSM node diagnostics
- `src/k8s_scale_test/anomaly.py` — where FailedCreatePodSandBox is detected

## Deliverables

1. A written analysis document covering:
   - Root cause analysis with source code references
   - Mathematical model of collision probability vs pod density
   - Threshold determination (at what N does P(collision) > 1%?)
   - Comparison of solution approaches with trade-offs
2. Updated KB entry with accurate information
3. Recommended next steps (which solution to implement first)
