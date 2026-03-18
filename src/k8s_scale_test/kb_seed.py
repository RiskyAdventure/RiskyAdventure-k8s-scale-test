"""Seed data loader for the Known Issues Knowledge Base.

Populates the KB with 12 known EKS scale-test failure patterns migrated
from the hardcoded domain knowledge in scale-test-observability.md.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from k8s_scale_test.models import AffectedVersions, KBEntry, Severity, Signature

if TYPE_CHECKING:
    from k8s_scale_test.kb_store import KBStore

logger = logging.getLogger(__name__)


def _build_seed_entries() -> list[KBEntry]:
    """Return the 12 seed KBEntry objects."""
    now = datetime.now(timezone.utc)

    return [
        KBEntry(
            entry_id="ipamd-mac-collision",
            title="VPC CNI MAC Address Collision at High Pod Density",
            category="networking",
            signature=Signature(
                event_reasons=["FailedCreatePodSandBox"],
                log_patterns=[
                    "failed to generate Unique MAC",
                    "results may be incomplete or inconsistent",
                ],
                metric_conditions=[],
                resource_kinds=["Pod"],
            ),
            root_cause=(
                "At high pod density (>100 pods/node), the VPC CNI's "
                "generateUniqueRandomMAC() function fails due to persistent "
                "netlink dump interruptions (NLM_F_DUMP_INTR). The function "
                "calls LinkList() to enumerate existing MACs. During "
                "concurrent pod creation, the kernel's netlink dump is "
                "interrupted by interface changes, returning "
                "ErrDumpInterrupted ('results may be incomplete or "
                "inconsistent'). The VPC CNI wraps LinkList() with "
                "retryOnErrDumpInterrupted (5 retries, 100ms delay), but "
                "under extreme concurrency (140+ pods being created "
                "simultaneously on a node), the dump interruption persists "
                "across all 5 retries. The error propagates up as "
                "'failed to generate Unique MAC addr for host side veth: "
                "...results may be incomplete or inconsistent'. This is "
                "NOT a birthday paradox — the 46-bit locally-administered "
                "MAC space (2^46 addresses) makes random collision "
                "astronomically unlikely. The root cause is sustained "
                "netlink dump interruption under concurrent veth creation "
                "that exceeds the retry budget. Pods eventually succeed "
                "on kubelet retry once interface churn subsides."
            ),
            recommended_actions=[
                "Reduce maxPods per node to 110 to lower concurrent veth "
                "creation pressure (tested threshold: collisions start at "
                "~120+ pods/node with concurrent creation)",
                "Use larger instance types to spread pods across more nodes "
                "and reduce per-node density",
                "Monitor per-node pod density and alert when approaching "
                "120 pods/node (add ObservabilityScanner query)",
                "File upstream issue on aws/amazon-vpc-cni-k8s to increase "
                "MAX_MAC_GENERATION_ATTEMPTS or implement retry-with-backoff "
                "in generateUniqueRandomMAC()",
                "File upstream issue to handle NLM_F_DUMP_INTR by retrying "
                "the LinkList() call when the dump is interrupted",
            ],
            severity=Severity.CRITICAL,
            affected_versions=[
                AffectedVersions(
                    component="vpc-cni",
                    min_version=None,
                    max_version=None,
                    fixed_in=None,
                ),
            ],
            created_at=now,
            last_seen=now,
            occurrence_count=0,
            status="active",
            review_notes=(
                "Observed at VPC CNI v1.20.4-eksbuild.2 on EKS cluster "
                "tf-shane (March 2026). Previous KB claimed fixed in "
                "v1.12.4 — this is WRONG. The v1.12.4 fix may have "
                "addressed a different variant but the netlink dump "
                "interruption persists under high concurrency. Current "
                "master has retryOnErrDumpInterrupted (5 retries, 100ms "
                "delay) wrapping LinkList() — unclear if this existed in "
                "v1.20.4 or was added later. Either way, the retry budget "
                "is insufficient at 140 pods/node. Source code confirmed "
                "in driver.go: generateUniqueRandomMAC() with "
                "MAX_MAC_GENERATION_ATTEMPTS=10, and netlinkwrapper with "
                "retryOnErrDumpInterrupted maxAttempts=5. Error path: "
                "LinkList() fails persistently → error propagates as "
                "'results may be incomplete or inconsistent' (the exact "
                "errDumpInterrupted.Error() string from vishvananda/"
                "netlink v1.3.1). Impact: 3,673 FailedCreatePodSandBox "
                "events across 2,884 pods in a 30K pod scale test (244 "
                "nodes, ~140 pods/node on i4i.8xlarge with prefix "
                "delegation). Adds 3-5 minutes of tail latency. See Go "
                "issue #52137 for the underlying NLM_F_DUMP_INTR bug."
            ),
            alternative_explanations=[
                "Birthday paradox in MAC space — RULED OUT: 310 interfaces "
                "in 2^46 space gives P(collision)=6.8e-10 per attempt, "
                "P(10 consecutive)≈0",
                "IPAMD IP exhaustion — RULED OUT: subnet IPs were plentiful "
                "(113K available), prefix delegation enabled",
                "Stale ENI state — UNLIKELY: restarting aws-node does not "
                "address the netlink race condition",
            ],
            checkpoint_questions=[
                "What is the per-node pod count when collisions start? "
                "Query AMP: max(kubelet_running_pods) by (node)",
                "Are IPAMD logs showing the specific 'failed to generate "
                "unique mac after 10 attempts' message?",
                "Does reducing maxPods to 110 eliminate the collisions "
                "entirely, or just reduce frequency?",
                "Is the netlink socket receive buffer size tunable on "
                "these nodes (sysctl net.core.rmem_max)?",
            ],
        ),
        KBEntry(
            entry_id="ipamd-ip-exhaustion",
            title="VPC CNI IP Address Exhaustion",
            category="networking",
            signature=Signature(
                event_reasons=["FailedCreatePodSandBox"],
                log_patterns=["no available IP/prefix", "failed to allocate IP"],
                metric_conditions=[],
                resource_kinds=["Pod"],
            ),
            root_cause=(
                "The VPC CNI plugin has exhausted all available IP addresses "
                "or prefixes on the node ENIs. New pods cannot be scheduled "
                "because no IPs are available for assignment."
            ),
            recommended_actions=[
                "Check subnet CIDR utilization and expand if needed",
                "Enable prefix delegation to increase IPs per ENI",
                "Review WARM_IP_TARGET and MINIMUM_IP_TARGET settings",
                "Scale out to additional nodes to distribute IP demand",
            ],
            severity=Severity.CRITICAL,
            affected_versions=[
                AffectedVersions(component="vpc-cni"),
            ],
            created_at=now,
            last_seen=now,
            occurrence_count=0,
            status="active",
        ),
        KBEntry(
            entry_id="subnet-ip-exhaustion",
            title="Subnet IP Address Exhaustion Preventing ENI Attachment",
            category="networking",
            signature=Signature(
                event_reasons=["FailedCreatePodSandBox"],
                log_patterns=["failed to attach ENI"],
                metric_conditions=[],
                resource_kinds=["Pod"],
            ),
            root_cause=(
                "The subnet has run out of available IP addresses, preventing "
                "the VPC CNI from attaching new ENIs to nodes. This blocks pod "
                "networking setup at the infrastructure level."
            ),
            recommended_actions=[
                "Check subnet IP availability via AWS console or CLI",
                "Add secondary CIDR blocks to the VPC",
                "Create new subnets in the VPC for pod networking",
                "Enable prefix delegation to reduce ENI consumption",
            ],
            severity=Severity.CRITICAL,
            affected_versions=[
                AffectedVersions(component="vpc-cni"),
            ],
            created_at=now,
            last_seen=now,
            occurrence_count=0,
            status="active",
        ),
        KBEntry(
            entry_id="coredns-bottleneck",
            title="CoreDNS Bottleneck Under High Pod Density",
            category="networking",
            signature=Signature(
                event_reasons=[],
                log_patterns=["SERVFAIL", "i/o timeout"],
                metric_conditions=[],
                resource_kinds=["Pod", "Deployment"],
            ),
            root_cause=(
                "CoreDNS cannot keep up with DNS query volume at high pod "
                "density. Queries time out or return SERVFAIL, causing "
                "application-level failures for any service relying on "
                "cluster DNS resolution."
            ),
            recommended_actions=[
                "Scale CoreDNS replicas proportionally to pod count",
                "Enable NodeLocal DNSCache to reduce CoreDNS load",
                "Check CoreDNS memory and CPU limits",
                "Review CoreDNS forward plugin configuration for upstream timeouts",
            ],
            severity=Severity.WARNING,
            affected_versions=[
                AffectedVersions(component="coredns"),
            ],
            created_at=now,
            last_seen=now,
            occurrence_count=0,
            status="active",
        ),
        KBEntry(
            entry_id="karpenter-capacity",
            title="Karpenter Insufficient Capacity Error",
            category="capacity",
            signature=Signature(
                event_reasons=["InsufficientCapacityError", "TerminationGracePeriodExpiring"],
                log_patterns=[],
                metric_conditions=[],
                resource_kinds=["NodeClaim", "NodePool"],
            ),
            root_cause=(
                "Karpenter cannot provision nodes because the requested "
                "instance types are unavailable in the target AZs. This "
                "commonly occurs during large-scale tests when EC2 capacity "
                "is constrained for the selected instance families."
            ),
            recommended_actions=[
                "Diversify instance types in NodePool requirements",
                "Spread across multiple availability zones",
                "Check EC2 service quotas for the account",
                "Use Karpenter consolidation policy to optimize existing capacity",
            ],
            severity=Severity.CRITICAL,
            affected_versions=[
                AffectedVersions(component="karpenter"),
            ],
            created_at=now,
            last_seen=now,
            occurrence_count=0,
            status="active",
        ),
        KBEntry(
            entry_id="image-pull-throttle",
            title="Container Image Pull Throttling",
            category="runtime",
            signature=Signature(
                event_reasons=["Failed", "ErrImagePull"],
                log_patterns=["pull QPS exceeded"],
                metric_conditions=[],
                resource_kinds=["Pod"],
            ),
            root_cause=(
                "The container runtime is being throttled by the image "
                "registry due to excessive pull requests. At scale, many "
                "nodes pulling the same image simultaneously exceeds the "
                "registry rate limit."
            ),
            recommended_actions=[
                "Use image caching or pre-pull DaemonSets",
                "Configure registry mirror or pull-through cache in ECR",
                "Stagger deployments to reduce concurrent pull pressure",
                "Check containerd max-concurrent-downloads setting",
            ],
            severity=Severity.WARNING,
            affected_versions=[
                AffectedVersions(component="containerd"),
            ],
            created_at=now,
            last_seen=now,
            occurrence_count=0,
            status="active",
        ),
        KBEntry(
            entry_id="oom-kill",
            title="Pod OOM Kill and Eviction",
            category="runtime",
            signature=Signature(
                event_reasons=["OOMKilling", "Evicted"],
                log_patterns=["oom", "kill"],
                metric_conditions=[],
                resource_kinds=["Pod", "Node"],
            ),
            root_cause=(
                "Pods are exceeding their memory limits and being OOM-killed "
                "by the kernel, or nodes are under memory pressure causing "
                "kubelet to evict pods. Common during scale tests when "
                "aggregate memory demand exceeds node capacity."
            ),
            recommended_actions=[
                "Review and increase pod memory limits",
                "Check node memory utilization and allocatable resources",
                "Tune kubelet eviction thresholds if too aggressive",
                "Use Vertical Pod Autoscaler to right-size memory requests",
            ],
            severity=Severity.CRITICAL,
            affected_versions=[
                AffectedVersions(component="eks"),
            ],
            created_at=now,
            last_seen=now,
            occurrence_count=0,
            status="active",
        ),
        KBEntry(
            entry_id="disk-pressure",
            title="Node Disk Pressure Causing Pod Eviction",
            category="storage",
            signature=Signature(
                event_reasons=["Evicted"],
                log_patterns=["disk pressure"],
                metric_conditions=[],
                resource_kinds=["Pod", "Node"],
            ),
            root_cause=(
                "Node local disk is running low, triggering the kubelet "
                "disk pressure condition. Pods are evicted to reclaim disk "
                "space. Common when container logs, image layers, or emptyDir "
                "volumes consume excessive disk."
            ),
            recommended_actions=[
                "Increase node root volume size",
                "Configure log rotation and container log max-size",
                "Clean up unused container images via garbage collection settings",
                "Use ephemeral-storage resource limits on pods",
            ],
            severity=Severity.WARNING,
            affected_versions=[
                AffectedVersions(component="eks"),
            ],
            created_at=now,
            last_seen=now,
            occurrence_count=0,
            status="active",
        ),
        KBEntry(
            entry_id="ec2-api-throttle",
            title="EC2 API Throttling Affecting VPC CNI Operations",
            category="networking",
            signature=Signature(
                event_reasons=[],
                log_patterns=["throttl", "rate exceeded", "requestlimitexceeded"],
                metric_conditions=[],
                resource_kinds=["Pod", "Node"],
            ),
            root_cause=(
                "EC2 API calls from the VPC CNI plugin are being throttled "
                "due to exceeding the account-level API rate limits. This "
                "prevents ENI attachment and IP allocation, blocking pod "
                "networking setup."
            ),
            recommended_actions=[
                "Request EC2 API rate limit increase via AWS support",
                "Enable VPC CNI prefix delegation to reduce API calls",
                "Stagger node scaling to reduce concurrent EC2 API calls",
                "Check WARM_ENI_TARGET to minimize unnecessary ENI allocations",
            ],
            severity=Severity.WARNING,
            affected_versions=[
                AffectedVersions(component="vpc-cni"),
            ],
            created_at=now,
            last_seen=now,
            occurrence_count=0,
            status="active",
        ),
        KBEntry(
            entry_id="nvme-disk-init",
            title="NVMe Disk Not Initialized on Instance Start",
            category="storage",
            signature=Signature(
                event_reasons=["InvalidDiskCapacity"],
                log_patterns=["NVMe not initialized"],
                metric_conditions=[],
                resource_kinds=["Node"],
            ),
            root_cause=(
                "NVMe instance store volumes on i4i (and similar) instances "
                "are not initialized at boot time. The kubelet reports invalid "
                "disk capacity because the NVMe devices have not been "
                "formatted or mounted before the node joins the cluster."
            ),
            recommended_actions=[
                "Add a bootstrap script to format and mount NVMe volumes at boot",
                "Use EC2 user data or a DaemonSet to initialize NVMe disks",
                "Verify instance store volume mount points in node configuration",
            ],
            severity=Severity.WARNING,
            affected_versions=[
                AffectedVersions(component="eks"),
            ],
            created_at=now,
            last_seen=now,
            occurrence_count=0,
            status="active",
        ),
        KBEntry(
            entry_id="kyverno-webhook-failure",
            title="Kyverno Admission Webhook Failure Blocking Pod Creation",
            category="control-plane",
            signature=Signature(
                event_reasons=["FailedCreate"],
                log_patterns=["failed calling webhook.*kyverno"],
                metric_conditions=[],
                resource_kinds=["Pod", "ReplicaSet"],
            ),
            root_cause=(
                "The Kyverno admission webhook is failing or unreachable, "
                "causing the API server to reject pod creation requests. "
                "This can happen when Kyverno pods are not ready, overloaded, "
                "or experiencing network issues."
            ),
            recommended_actions=[
                "Check Kyverno pod health and readiness",
                "Scale Kyverno replicas for high-throughput clusters",
                "Configure webhook failure policy to Ignore for non-critical policies",
                "Review Kyverno resource limits and increase if needed",
            ],
            severity=Severity.CRITICAL,
            affected_versions=[
                AffectedVersions(component="kyverno"),
            ],
            created_at=now,
            last_seen=now,
            occurrence_count=0,
            status="active",
        ),
        KBEntry(
            entry_id="systemd-cgroup-timeout",
            title="Systemd Cgroup Timeout During Pod Container Creation",
            category="runtime",
            signature=Signature(
                event_reasons=["FailedCreatePodContainer"],
                log_patterns=["Timeout waiting for systemd"],
                metric_conditions=[],
                resource_kinds=["Pod"],
            ),
            root_cause=(
                "The container runtime times out waiting for systemd to "
                "create the cgroup for a new container. This occurs under "
                "high pod churn when systemd's D-Bus interface becomes a "
                "bottleneck, serializing cgroup operations."
            ),
            recommended_actions=[
                "Reduce pod creation rate to lower systemd pressure",
                "Check systemd version and upgrade if known fixes exist",
                "Monitor systemd D-Bus queue depth on affected nodes",
                "Consider using cgroupfs driver instead of systemd if compatible",
            ],
            severity=Severity.WARNING,
            affected_versions=[
                AffectedVersions(component="containerd"),
            ],
            created_at=now,
            last_seen=now,
            occurrence_count=0,
            status="active",
        ),
    ]


def load_seed_entries(kb_store: KBStore) -> list[KBEntry]:
    """Write the 12 seed KB entries to DynamoDB + S3 via KBStore.save().

    Args:
        kb_store: An initialized KBStore instance.

    Returns:
        The list of seed KBEntry objects that were saved.
    """
    entries = _build_seed_entries()
    for entry in entries:
        try:
            kb_store.save(entry)
            logger.info("Seeded KB entry: %s", entry.entry_id)
        except Exception:
            logger.exception("Failed to seed KB entry: %s", entry.entry_id)
            raise
    logger.info("Seeded %d KB entries", len(entries))
    return entries
