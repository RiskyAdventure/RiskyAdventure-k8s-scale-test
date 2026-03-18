# Next Session: Deep Investigation — VPC CNI MAC Address Collision at Scale

Read `.kiro/steering/engineering-rigor.md` before starting any work.

## Primary Goal

Solve the VPC CNI MAC address collision problem that adds 3-5 minutes of tail latency to 30K pod scale tests.

## Secondary Goal — Module Efficacy Test

Use this investigation as a live test of the monitoring and investigation pipeline we built. The modules should be doing the heavy lifting — you drive them, evaluate their output, and note where they fall short.

### How to Use the Pipeline

1. **Run a scale test** to reproduce the MAC collision (30K pods, same config as previous runs). Use the runbook at `.kiro/steering/run-scale-test.md`.

2. **Let the pipeline investigate first.** When FailedCreatePodSandBox events fire, the anomaly detector will run its full pipeline. Wait for the finding before doing your own investigation. Check:
   - Did the **AnomalyDetector** correctly identify MAC collision as the root cause? (Layer 4.5 AMP metrics, Layer 6 SSM diagnostics)
   - Did the **ObservabilityScanner** detect the issue proactively before the rate drop alert?
   - Did the **SharedContext** correlation match scanner findings to the anomaly alert? Were matches strong or weak? Why?
   - Did the **FindingReviewer** assign appropriate confidence? Were its alternative explanations useful? Were checkpoint questions actionable?
   - Did the **KB matcher** match the `ipamd-mac-collision` entry? What score?

3. **Use AMP/CloudWatch to dig deeper.** After the pipeline produces its finding:
   - Query AMP for per-node pod counts to identify which nodes hit the collision threshold
   - Query CloudWatch dataplane logs for the exact "failed to generate Unique MAC" error messages — extract node names, timestamps, retry counts
   - Use the EKS MCP tools to check node conditions and pod states on affected nodes

4. **Document pipeline gaps.** For each module, note:
   - What it found correctly
   - What it missed
   - What it got wrong
   - What additional data it should have collected but didn't
   - Specific improvements to make (new queries, better thresholds, missing evidence types)

### Pipeline Evaluation Checklist

After the investigation, answer these questions:

| Module | Question | Answer |
|--------|----------|--------|
| AnomalyDetector | Did it identify MAC collision as root cause? | |
| AnomalyDetector | Did the AMP layer (4.5) provide useful evidence? | |
| AnomalyDetector | Did SSM diagnostics (Layer 6) capture the IPAMD logs showing the collision? | |
| ObservabilityScanner | Did any Tier 1 query detect the issue before the rate drop? | |
| ObservabilityScanner | Should there be a new scanner query for per-node pod density? | |
| SharedContext | Did correlation match scanner findings to the alert? Strong or weak? | |
| SharedContext | Was the 120s correlation window appropriate? | |
| FindingReviewer | Was the confidence level appropriate? | |
| FindingReviewer | Were alternative explanations relevant? | |
| FindingReviewer | Were checkpoint questions actionable for this specific issue? | |
| KB Matcher | Did it match `ipamd-mac-collision`? What score? | |
| KB Matcher | Is the KB entry accurate enough to be useful? | |

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
- Use AMP to query per-node pod counts at the time of the collisions
- Check the IPAMD logs from our test runs:
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
- Add a new ObservabilityScanner query that tracks per-node pod density and alerts when approaching the collision threshold

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
- `src/k8s_scale_test/anomaly.py` — anomaly detector (evaluate its MAC collision detection)
- `src/k8s_scale_test/observability.py` — scanner (evaluate proactive detection, consider new queries)
- `src/k8s_scale_test/shared_context.py` — cross-source correlation (evaluate matching quality)
- `src/k8s_scale_test/reviewer.py` — skeptical review (evaluate confidence and alternatives)
- `src/k8s_scale_test/controller.py` — controller wiring (evaluate end-to-end pipeline)
- `.kiro/steering/run-scale-test.md` — runbook for launching tests
- `scale-test-results/2026-03-18_14-42-47/` — latest test run with MAC collisions
- `scale-test-results/2026-03-18_14-42-47/events.jsonl` — K8s events
- `scale-test-results/2026-03-18_14-42-47/findings/` — anomaly detector findings
- `scale-test-results/2026-03-18_14-42-47/diagnostics/` — SSM node diagnostics

## Deliverables

1. **MAC collision solution** — implement the best fix (or combination of fixes) and validate with a test run
2. **Updated KB entry** — accurate root cause, correct version info, threshold data, actionable recommendations
3. **Pipeline evaluation report** — fill in the evaluation checklist above, document gaps found, and file improvements as tasks or specs
4. **New scanner query** (if warranted) — a per-node pod density query that alerts before hitting the collision threshold
