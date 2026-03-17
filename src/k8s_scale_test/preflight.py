"""Preflight capacity validation before scale test execution."""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone

from k8s_scale_test.models import (
    EC2Quotas,
    GoNoGo,
    GoNoGoDecision,
    NodeClassConfig,
    NodePoolCapacity,
    NodePoolConfig,
    PodsPerNodeBreakdown,
    PodSizingRecommendation,
    PreflightReport,
    StressorSizing,
    SubnetIPInfo,
    TestConfig,
)

# Known instance type core counts for supported Karpenter node types.
INSTANCE_TYPE_CORES: dict[str, int] = {
    "c7g.2xlarge": 8,
    "m7g.2xlarge": 8,
    "r7g.4xlarge": 16,
    "r7g.8xlarge": 32,
    "i4i.8xlarge": 32,
    "i7i.8xlarge": 32,
    "i8i.8xlarge": 32,
}

# Known instance type memory in MiB.
INSTANCE_TYPE_MEMORY_MI: dict[str, int] = {
    "c7g.2xlarge": 16384,     # 16 GiB
    "m7g.2xlarge": 32768,     # 32 GiB
    "r7g.4xlarge": 131072,    # 128 GiB
    "r7g.8xlarge": 262144,    # 256 GiB
    "i4i.8xlarge": 262144,    # 256 GiB
    "i7i.8xlarge": 262144,    # 256 GiB
    "i8i.8xlarge": 262144,    # 256 GiB
}


def parse_cpu_millicores(value: str) -> int:
    """Parse a Kubernetes CPU resource string to millicores.

    Examples: "100m" → 100, "2" → 2000, "1.5" → 1500, "0" → 0
    """
    value = value.strip()
    if not value or value == "0":
        return 0
    if value.endswith("m"):
        return int(value[:-1])
    try:
        return int(float(value) * 1000)
    except (ValueError, TypeError):
        return 0


def parse_memory_mi(value: str) -> int:
    """Parse a Kubernetes memory resource string to MiB.

    Examples: "100Mi" → 100, "1Gi" → 1024, "512Ki" → 0 (rounds down),
    "5%" → 0 (percentages are not absolute values, return 0).
    """
    value = value.strip()
    if value.endswith("%"):
        # Percentage-based eviction thresholds can't be converted to absolute MiB
        # without knowing total memory. Return 0 and let caller handle it.
        return 0
    if value.endswith("Gi"):
        return int(float(value[:-2]) * 1024)
    if value.endswith("Mi"):
        return int(float(value[:-2]))
    if value.endswith("Ki"):
        return int(float(value[:-2]) / 1024)
    # Plain bytes
    try:
        return int(int(value) / (1024 * 1024))
    except (ValueError, TypeError):
        return 0


class PreflightChecker:
    """One-time validation of cluster capacity before test execution."""

    def __init__(self, config: TestConfig, k8s_client=None, aws_client=None):
        self.config = config
        self.k8s_client = k8s_client
        self.aws_client = aws_client

    def _calculate_pods_per_node(
        self,
        instance_type: str,
        node_class: NodeClassConfig,
        nodepool_name: str,
        total_cores: int,
    ) -> PodsPerNodeBreakdown:
        """Compute effective max pods per node for a given instance type.

        effective_max_pods = min(maxPods, total_cores * podsPerCore)

        Parameters
        ----------
        instance_type:
            EC2 instance type (e.g. ``c7g.16xlarge``).
        node_class:
            The EC2NodeClass kubelet configuration containing maxPods,
            podsPerCore, and reservation settings.
        nodepool_name:
            Name of the Karpenter NodePool this instance belongs to.
        total_cores:
            Number of vCPUs for *instance_type*.
        """
        pods_per_core_limit = total_cores * node_class.pods_per_core if node_class.pods_per_core > 0 else node_class.max_pods
        effective_max_pods = min(node_class.max_pods, pods_per_core_limit)

        return PodsPerNodeBreakdown(
            instance_type=instance_type,
            nodepool_name=nodepool_name,
            total_cores=total_cores,
            max_pods_setting=node_class.max_pods,
            pods_per_core_setting=node_class.pods_per_core,
            pods_per_core_limit=pods_per_core_limit,
            effective_max_pods=effective_max_pods,
            system_reserved_cpu=node_class.system_reserved_cpu,
            kube_reserved_cpu=node_class.kube_reserved_cpu,
            system_reserved_memory=node_class.system_reserved_memory,
            kube_reserved_memory=node_class.kube_reserved_memory,
            eviction_hard_memory=node_class.eviction_hard_memory,
        )

    def _calculate_optimal_pod_sizing(
        self,
        instance_type: str,
        node_class: NodeClassConfig,
        effective_max_pods: int,
        total_cores: int,
    ) -> PodSizingRecommendation:
        """Compute optimal per-pod resource requests to maximize density.

        Allocatable resources = total - system_reserved - kube_reserved
        (memory also subtracts eviction_hard threshold).

        The recommended per-pod request divides allocatable evenly across
        effective_max_pods so resource limits are not the bottleneck.

        Parameters
        ----------
        instance_type:
            EC2 instance type.
        node_class:
            EC2NodeClass kubelet configuration.
        effective_max_pods:
            Result of ``min(maxPods, cores * podsPerCore)``.
        total_cores:
            vCPU count for *instance_type*.
        """
        total_cpu_mc = total_cores * 1000
        total_mem_mi = INSTANCE_TYPE_MEMORY_MI.get(instance_type, total_cores * 2048)

        sys_cpu = parse_cpu_millicores(node_class.system_reserved_cpu)
        kube_cpu = parse_cpu_millicores(node_class.kube_reserved_cpu)
        sys_mem = parse_memory_mi(node_class.system_reserved_memory)
        kube_mem = parse_memory_mi(node_class.kube_reserved_memory)
        evict_mem = parse_memory_mi(node_class.eviction_hard_memory)

        allocatable_cpu = max(total_cpu_mc - sys_cpu - kube_cpu, 0)
        allocatable_mem = max(total_mem_mi - sys_mem - kube_mem - evict_mem, 0)

        if effective_max_pods <= 0:
            return PodSizingRecommendation(
                instance_type=instance_type,
                allocatable_cpu_millicores=allocatable_cpu,
                allocatable_memory_mi=allocatable_mem,
                recommended_cpu_request="0m",
                recommended_memory_request="0Mi",
                max_pods_by_cpu=0,
                max_pods_by_memory=0,
                effective_density_limit=0,
            )

        per_pod_cpu = max(allocatable_cpu // effective_max_pods, 1)
        per_pod_mem = max(allocatable_mem // effective_max_pods, 1)

        max_pods_by_cpu = allocatable_cpu // per_pod_cpu if per_pod_cpu > 0 else 0
        max_pods_by_memory = allocatable_mem // per_pod_mem if per_pod_mem > 0 else 0

        effective_density_limit = min(
            effective_max_pods, max_pods_by_cpu, max_pods_by_memory
        )

        return PodSizingRecommendation(
            instance_type=instance_type,
            allocatable_cpu_millicores=allocatable_cpu,
            allocatable_memory_mi=allocatable_mem,
            recommended_cpu_request=f"{per_pod_cpu}m",
            recommended_memory_request=f"{per_pod_mem}Mi",
            max_pods_by_cpu=max_pods_by_cpu,
            max_pods_by_memory=max_pods_by_memory,
            effective_density_limit=effective_density_limit,
        )

    def _calculate_nodepool_capacity(
        self,
        pool: NodePoolConfig,
        breakdowns: list[PodsPerNodeBreakdown],
    ) -> NodePoolCapacity:
        """Calculate the maximum pod capacity for a single NodePool.

        For each instance type in the pool, computes:
          max_nodes = floor(pool.cpu_limit / vcpus_per_instance)
          max_pods  = max_nodes * effective_max_pods

        Parameters
        ----------
        pool:
            Karpenter NodePool configuration.
        breakdowns:
            Pre-computed PodsPerNodeBreakdown for each instance type in the pool.
        """
        vcpus_per_instance: dict[str, int] = {}
        max_nodes_per_type: dict[str, int] = {}
        max_pods_per_type: dict[str, int] = {}

        breakdown_map = {b.instance_type: b for b in breakdowns}

        for itype in pool.instance_types:
            cores = INSTANCE_TYPE_CORES.get(itype, 0)
            vcpus_per_instance[itype] = cores

            if cores <= 0:
                max_nodes_per_type[itype] = 0
                max_pods_per_type[itype] = 0
                continue

            max_nodes = pool.cpu_limit // cores
            max_nodes_per_type[itype] = max_nodes

            bd = breakdown_map.get(itype)
            effective = bd.effective_max_pods if bd else 0
            max_pods_per_type[itype] = max_nodes * effective

        total_max_pods = sum(max_pods_per_type.values())

        return NodePoolCapacity(
            name=pool.name,
            instance_types=pool.instance_types,
            vcpus_per_instance=vcpus_per_instance,
            max_nodes_per_type=max_nodes_per_type,
            max_pods_per_type=max_pods_per_type,
            total_max_pods=total_max_pods,
        )

    @staticmethod
    def calculate_total_ip_availability(
        subnets: list[SubnetIPInfo],
    ) -> int:
        """Sum available IPs across all subnets."""
        return sum(s.available_ips for s in subnets)

    @staticmethod
    def calculate_max_achievable_pods(
        pool_capacities: list[NodePoolCapacity],
        total_available_ips: int,
    ) -> int:
        """Compute max achievable pods capped by min(pod capacity, IPs)."""
        total_pod_capacity = sum(pc.total_max_pods for pc in pool_capacities)
        return min(total_pod_capacity, total_available_ips)

    def _evaluate_go_nogo(
        self,
        target_pods: int,
        max_achievable_pods: int,
        total_available_ips: int,
        ec2_quotas: EC2Quotas,
        pool_capacities: list[NodePoolCapacity],
    ) -> GoNoGoDecision:
        """Produce a GO / NO_GO decision based on capacity constraints.

        Checks:
        1. Target pods vs max achievable pods (combined NodePool capacity)
        2. Target pods vs available IPs
        3. EC2 vCPU quota headroom (warn if usage > 80%)
        """
        constraints_checked: list[str] = []
        blocking: list[str] = []
        recommendations: list[str] = []

        # 1. NodePool pod capacity
        constraints_checked.append("nodepool_pod_capacity")
        total_pool_pods = sum(pc.total_max_pods for pc in pool_capacities)
        if target_pods > total_pool_pods:
            blocking.append(
                f"Target {target_pods} exceeds NodePool pod capacity "
                f"{total_pool_pods} (shortfall: {target_pods - total_pool_pods})"
            )
            recommendations.append(
                "Increase NodePool CPU limits or add more instance types."
            )

        # 2. IP availability
        constraints_checked.append("ip_availability")
        if target_pods > total_available_ips:
            blocking.append(
                f"Target {target_pods} exceeds available IPs "
                f"{total_available_ips} (shortfall: {target_pods - total_available_ips})"
            )
            recommendations.append(
                "Add subnets or increase CIDR ranges for more IPs."
            )

        # 3. EC2 vCPU quota
        constraints_checked.append("ec2_vcpu_quota")
        if ec2_quotas.usage_pct > 80.0:
            recommendations.append(
                f"EC2 vCPU usage at {ec2_quotas.usage_pct:.1f}% "
                f"({ec2_quotas.current_vcpu_usage}/{ec2_quotas.vcpu_quota}). "
                "Consider requesting a quota increase."
            )
        if ec2_quotas.headroom_vcpus <= 0:
            blocking.append(
                f"No EC2 vCPU headroom (usage: {ec2_quotas.current_vcpu_usage}, "
                f"quota: {ec2_quotas.vcpu_quota})"
            )

        decision = GoNoGo.NO_GO if blocking else GoNoGo.GO
        return GoNoGoDecision(
            decision=decision,
            constraints_checked=constraints_checked,
            blocking_constraints=blocking,
            recommendations=recommendations,
        )

    # ------------------------------------------------------------------
    # AWS / K8s query methods (used by run())
    # ------------------------------------------------------------------

    async def _get_ec2_quotas(self) -> EC2Quotas:
        """Query EC2 service quotas via standard AWS API (not Beta).

        Uses K8s node labels to count vCPUs instead of slow EC2 pagination.
        """
        log = logging.getLogger(__name__)
        try:
            # service-quotas uses standard AWS endpoint, not Beta
            sq_client = self.aws_client.client(
                "service-quotas",
            ) if hasattr(self.aws_client, "client") else self.aws_client

            # L-1216C47A is the quota code for "Running On-Demand Standard instances"
            resp = sq_client.get_service_quota(
                ServiceCode="ec2",
                QuotaCode="L-1216C47A",
            )
            quota_value = int(resp["Quota"]["Value"])

            # Count current vCPUs from K8s nodes (much faster than EC2 pagination)
            current_vcpus = 0
            try:
                v1 = self.k8s_client.CoreV1Api()
                nodes = v1.list_node(watch=False)
                for node in nodes.items:
                    labels = node.metadata.labels or {}
                    itype = labels.get("node.kubernetes.io/instance-type", "")
                    cores = INSTANCE_TYPE_CORES.get(itype, 0)
                    if cores > 0:
                        current_vcpus += cores
                    elif node.status and node.status.capacity:
                        # Fallback: read from node capacity
                        cpu_str = node.status.capacity.get("cpu", "0")
                        current_vcpus += int(cpu_str)
            except Exception as exc:
                log.warning("K8s node vCPU count failed, falling back to 0: %s", exc)

            headroom = quota_value - current_vcpus
            usage_pct = (current_vcpus / quota_value * 100) if quota_value > 0 else 100.0

            return EC2Quotas(
                current_vcpu_usage=current_vcpus,
                vcpu_quota=quota_value,
                headroom_vcpus=headroom,
                usage_pct=usage_pct,
            )
        except Exception as exc:
            log.warning("EC2 quota query failed: %s", exc)
            return EC2Quotas(
                current_vcpu_usage=0,
                vcpu_quota=0,
                headroom_vcpus=0,
                usage_pct=0.0,
            )

    async def _get_subnet_ip_availability(self) -> list[SubnetIPInfo]:
        """Query subnet IP availability for Karpenter-discovered subnets."""
        log = logging.getLogger(__name__)
        try:
            # EC2 uses standard AWS endpoint, not Beta
            ec2 = self.aws_client.client(
                "ec2",
            ) if hasattr(self.aws_client, "client") else self.aws_client

            resp = ec2.describe_subnets(
                Filters=[
                    {"Name": "tag-key", "Values": ["karpenter.sh/discovery"]},
                ]
            )
            subnets: list[SubnetIPInfo] = []
            for s in resp.get("Subnets", []):
                subnets.append(SubnetIPInfo(
                    subnet_id=s["SubnetId"],
                    availability_zone=s["AvailabilityZone"],
                    available_ips=s["AvailableIpAddressCount"],
                    cidr_block=s["CidrBlock"],
                ))
            return subnets
        except Exception as exc:
            log.warning("Subnet IP query failed: %s", exc)
            return []

    async def _get_nodepool_configs(self) -> list[NodePoolConfig]:
        """Read Karpenter NodePool CRDs from the K8s API."""
        log = logging.getLogger(__name__)
        try:
            api = self.k8s_client.CustomObjectsApi()
            resp = api.list_cluster_custom_object(
                group="karpenter.sh",
                version="v1",
                plural="nodepools",
            )
            pools: list[NodePoolConfig] = []
            for item in resp.get("items", []):
                spec = item.get("spec", {})
                template = spec.get("template", {}).get("spec", {})
                reqs = template.get("requirements", [])

                instance_types: list[str] = []
                architecture = "amd64"
                capacity_type = "on-demand"
                for req in reqs:
                    key = req.get("key", "")
                    values = req.get("values", [])
                    if key == "node.kubernetes.io/instance-type":
                        instance_types = values
                    elif key == "kubernetes.io/arch":
                        architecture = values[0] if values else "amd64"
                    elif key == "karpenter.sh/capacity-type":
                        capacity_type = values[0] if values else "on-demand"

                limits = spec.get("limits", {})
                cpu_limit = int(limits.get("cpu", 0))
                mem_str = str(limits.get("memory", "0Gi"))
                mem_gi = int(mem_str.replace("Gi", "")) if "Gi" in mem_str else 0

                node_class_ref = template.get("nodeClassRef", {})
                node_class_name = node_class_ref.get("name", "")

                pools.append(NodePoolConfig(
                    name=item["metadata"]["name"],
                    cpu_limit=cpu_limit,
                    memory_limit_gi=mem_gi,
                    instance_types=instance_types,
                    architecture=architecture,
                    capacity_type=capacity_type,
                    node_class_name=node_class_name,
                ))
            return pools
        except Exception as exc:
            log.warning("NodePool query failed: %s", exc)
            return []

    async def _get_nodeclass_configs(self) -> list[NodeClassConfig]:
        """Read Karpenter EC2NodeClass CRDs from the K8s API."""
        log = logging.getLogger(__name__)
        try:
            api = self.k8s_client.CustomObjectsApi()
            resp = api.list_cluster_custom_object(
                group="karpenter.k8s.aws",
                version="v1",
                plural="ec2nodeclasses",
            )
            classes: list[NodeClassConfig] = []
            for item in resp.get("items", []):
                spec = item.get("spec", {})
                kubelet = spec.get("kubelet", {}) or spec.get("kubeletConfiguration", {})

                classes.append(NodeClassConfig(
                    name=item["metadata"]["name"],
                    max_pods=kubelet.get("maxPods", 110),
                    pods_per_core=kubelet.get("podsPerCore", 0),
                    system_reserved_cpu=kubelet.get("systemReserved", {}).get("cpu", "0m"),
                    system_reserved_memory=kubelet.get("systemReserved", {}).get("memory", "0Mi"),
                    kube_reserved_cpu=kubelet.get("kubeReserved", {}).get("cpu", "0m"),
                    kube_reserved_memory=kubelet.get("kubeReserved", {}).get("memory", "0Mi"),
                    eviction_hard_memory=kubelet.get("evictionHard", {}).get(
                        "memory.available", "100Mi"
                    ),
                    subnet_discovery_tag="karpenter.sh/discovery",
                ))
            return classes
        except Exception as exc:
            log.warning("EC2NodeClass query failed: %s", exc)
            return []

    def _compute_stressor_sizing(
        self,
        pod_sizing: PodSizingRecommendation,
        daemonset_count: int,
        effective_max_pods: int,
        instance_type: str,
        cpu_limit_multiplier: float = 2.0,
        memory_limit_multiplier: float = 1.5,
    ) -> StressorSizing:
        """Compute per-pod resource requests for stressor deployments.

        Divides allocatable resources evenly across the pod ceiling
        (effective_max_pods - daemonset_count) so each stressor type
        packs to the node's pod limit.
        """
        pod_ceiling = max(effective_max_pods - daemonset_count, 1)
        cpu_req = max(pod_sizing.allocatable_cpu_millicores // pod_ceiling, 1)
        mem_req = max(pod_sizing.allocatable_memory_mi // pod_ceiling, 1)
        return StressorSizing(
            cpu_request_millicores=cpu_req,
            memory_request_mi=mem_req,
            pod_ceiling=pod_ceiling,
            instance_type=instance_type,
            allocatable_cpu_millicores=pod_sizing.allocatable_cpu_millicores,
            allocatable_memory_mi=pod_sizing.allocatable_memory_mi,
            daemonset_count=daemonset_count,
            cpu_limit_multiplier=cpu_limit_multiplier,
            memory_limit_multiplier=memory_limit_multiplier,
        )
    async def _get_node_allocatable(self, instance_type: str) -> tuple[int, int] | None:
        """Query actual allocatable CPU/memory from a live node of the given instance type.

        Returns (cpu_millicores, memory_mi) or None if no matching node is found.
        This is more accurate than computing from NodeClass CRD reservations because
        kubelet reservations may be set via user data / launch template rather than
        the NodeClass spec.
        """
        log = logging.getLogger(__name__)
        if not self.k8s_client:
            return None
        try:
            v1 = self.k8s_client.CoreV1Api()
            nodes = v1.list_node(
                label_selector=f"node.kubernetes.io/instance-type={instance_type}",
                limit=1,
                watch=False,
            )
            if not nodes.items:
                return None
            node = nodes.items[0]
            alloc = node.status.allocatable or {}

            # Parse CPU
            cpu_str = alloc.get("cpu", "0")
            if cpu_str.endswith("m"):
                cpu_m = int(cpu_str[:-1])
            else:
                cpu_m = int(cpu_str) * 1000

            # Parse memory (may be Ki, Mi, Gi, or raw bytes)
            mem_str = str(alloc.get("memory", "0"))
            if mem_str.endswith("Ki"):
                mem_mi = int(mem_str[:-2]) // 1024
            elif mem_str.endswith("Mi"):
                mem_mi = int(mem_str[:-2])
            elif mem_str.endswith("Gi"):
                mem_mi = int(float(mem_str[:-2]) * 1024)
            else:
                mem_mi = int(mem_str) // (1024 * 1024)

            log.info("  Live node allocatable for %s: cpu=%dm, memory=%dMi (from %s)",
                     instance_type, cpu_m, mem_mi, node.metadata.name)
            return cpu_m, mem_mi
        except Exception as exc:
            log.debug("Could not query live node allocatable for %s: %s", instance_type, exc)
            return None



    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    async def run(self) -> PreflightReport:
        """Preflight: can the cluster support these workloads at this scale?

        Checks:
        1. Subnet IPs — enough for all pods?
        2. vCPU quota — enough headroom for the nodes we'll need?
        3. NodePool CPU limits — enough budget?
        4. Pod ceiling — max_nodes × (maxPods - daemonset_slots) per pool
        """
        log = logging.getLogger(__name__)
        log.info("Starting preflight checks...")

        ec2_quotas = await self._get_ec2_quotas()
        subnet_ips = await self._get_subnet_ip_availability()
        total_ips = self.calculate_total_ip_availability(subnet_ips)
        pools = await self._get_nodepool_configs()
        node_classes = await self._get_nodeclass_configs()
        nc_by_name = {nc.name: nc for nc in node_classes}

        # Count DaemonSets (they consume pod slots on every node)
        daemonset_count = await self._count_daemonsets()

        # Compute stressor sizing from live cluster data
        stressor_sizing = None
        pod_sizing_recs = []
        pods_per_node_bds = []
        if pools and node_classes:
            # First, try to get live node allocatable from any instance type in the target pool.
            # This is more accurate than CRD-computed values because kubelet reservations
            # may be set via NodeConfig/user data rather than the EC2NodeClass spec.
            live_allocatable = None
            live_instance_type = None
            for pool in pools:
                if live_allocatable:
                    break
                # Warn if pool has mixed-size instance types — single sizing can't be optimal for all
                core_counts = {INSTANCE_TYPE_CORES.get(t, 0) for t in pool.instance_types if INSTANCE_TYPE_CORES.get(t, 0) > 0}
                if len(core_counts) > 1:
                    log.warning("  Pool %s has mixed-size instance types (%s) — sizing will target the first available",
                                pool.name, ", ".join(pool.instance_types))
                for itype in pool.instance_types:
                    live = await self._get_node_allocatable(itype)
                    if live:
                        live_allocatable = live
                        live_instance_type = itype
                        break

            for pool in pools:
                nc = nc_by_name.get(pool.node_class_name)
                if not nc:
                    continue
                for itype in pool.instance_types:
                    cores = INSTANCE_TYPE_CORES.get(itype, 0)
                    if cores <= 0:
                        continue
                    bd = self._calculate_pods_per_node(itype, nc, pool.name, cores)
                    pods_per_node_bds.append(bd)
                    ps = self._calculate_optimal_pod_sizing(itype, nc, bd.effective_max_pods, cores)

                    # Override with live node allocatable if available and same core count
                    # (nodes in the same pool share the same NodeConfig/kubelet reservations)
                    if live_allocatable and stressor_sizing is None:
                        live_cpu, live_mem = live_allocatable
                        live_cores = INSTANCE_TYPE_CORES.get(live_instance_type, 0)
                        if live_cores == cores and (live_cpu != ps.allocatable_cpu_millicores or live_mem != ps.allocatable_memory_mi):
                            log.info("  Overriding CRD allocatable (%dm/%dMi) with live node (%dm/%dMi) from %s",
                                     ps.allocatable_cpu_millicores, ps.allocatable_memory_mi,
                                     live_cpu, live_mem, live_instance_type)
                            ps = PodSizingRecommendation(
                                instance_type=ps.instance_type,
                                allocatable_cpu_millicores=live_cpu,
                                allocatable_memory_mi=live_mem,
                                recommended_cpu_request=ps.recommended_cpu_request,
                                recommended_memory_request=ps.recommended_memory_request,
                                max_pods_by_cpu=live_cpu // max(live_cpu // bd.effective_max_pods, 1) if bd.effective_max_pods > 0 else 0,
                                max_pods_by_memory=live_mem // max(live_mem // bd.effective_max_pods, 1) if bd.effective_max_pods > 0 else 0,
                                effective_density_limit=ps.effective_density_limit,
                            )

                    pod_sizing_recs.append(ps)
                    if stressor_sizing is None:
                        stressor_sizing = self._compute_stressor_sizing(
                            ps, daemonset_count, bd.effective_max_pods, itype,
                            cpu_limit_multiplier=self.config.cpu_limit_multiplier,
                            memory_limit_multiplier=self.config.memory_limit_multiplier,
                        )
            if stressor_sizing:
                log.info("  Stressor sizing: %dm CPU, %dMi memory per pod (pod_ceiling=%d, instance=%s)",
                         stressor_sizing.cpu_request_millicores, stressor_sizing.memory_request_mi,
                         stressor_sizing.pod_ceiling, stressor_sizing.instance_type)
        else:
            log.warning("Cannot compute stressor sizing: no NodeClass/NodePool data. Using manifest defaults.")

        # Calculate workload demand
        from k8s_scale_test.flux import FluxRepoReader
        reader = FluxRepoReader(self.config.flux_repo_path)
        deployments = reader.get_deployments()
        if self.config.include_apps:
            deployments = [d for d in deployments if d.name in set(self.config.include_apps)]
        if self.config.exclude_apps:
            deployments = [d for d in deployments if d.name not in set(self.config.exclude_apps)]

        # Calculate workload demand — use dynamic sizing if available
        if stressor_sizing:
            # Use computed sizing for all pods (all stressors get same per-pod resources)
            total_cpu_m = stressor_sizing.cpu_request_millicores * self.config.target_pods
            total_mem_mi = stressor_sizing.memory_request_mi * self.config.target_pods
            log.info("  Workload demand (dynamic sizing): %.1f vCPU, %.1f GiB memory, %d pods",
                     total_cpu_m / 1000, total_mem_mi / 1024, self.config.target_pods)
        else:
            # Fallback: use manifest resource requests (original behavior)
            n_deploys = len(deployments) or 1
            per_deploy = self.config.target_pods // n_deploys
            remainder = self.config.target_pods % n_deploys
            total_cpu_m = 0
            total_mem_mi = 0
            workload_summary = []
            for i, d in enumerate(deployments):
                replicas = per_deploy + (1 if i < remainder else 0)
                cpu_m = parse_cpu_millicores(d.resource_requests.get("cpu", "0"))
                mem_mi = parse_memory_mi(d.resource_requests.get("memory", "0"))
                total_cpu_m += cpu_m * replicas
                total_mem_mi += mem_mi * replicas
                workload_summary.append(f"{d.name}:{replicas}x({cpu_m}m,{mem_mi}Mi)")
            log.info("  Workloads: %s", ", ".join(workload_summary))
            log.info("  Total demand: %.1f vCPU, %.1f GiB memory, %d pods",
                     total_cpu_m / 1000, total_mem_mi / 1024, self.config.target_pods)

        total_cpu_cores = total_cpu_m / 1000

        constraints_checked = []
        blocking = []
        recommendations = []

        # 1. Subnet IPs
        constraints_checked.append("subnet_ips")
        if self.config.target_pods > total_ips:
            shortfall = self.config.target_pods - total_ips
            blocking.append(f"Need {self.config.target_pods} IPs, only {total_ips} available (shortfall: {shortfall})")
            recommendations.append("Add secondary CIDR blocks to VPC subnets")
        else:
            log.info("  Subnet IPs: %d available, %d needed — OK", total_ips, self.config.target_pods)

        # 2. vCPU quota
        constraints_checked.append("vcpu_quota")
        if ec2_quotas.vcpu_quota > 0:
            needed_vcpus = int(total_cpu_cores) + 100
            if needed_vcpus > ec2_quotas.headroom_vcpus:
                blocking.append(
                    f"Need ~{needed_vcpus} vCPUs, only {ec2_quotas.headroom_vcpus} headroom "
                    f"(usage: {ec2_quotas.current_vcpu_usage}/{ec2_quotas.vcpu_quota})")
                recommendations.append("Request EC2 vCPU quota increase")
            else:
                log.info("  vCPU quota: %d headroom, ~%d needed — OK",
                         ec2_quotas.headroom_vcpus, needed_vcpus)
            if ec2_quotas.usage_pct > 80:
                recommendations.append(f"vCPU usage at {ec2_quotas.usage_pct:.0f}%")

        # 3. NodePool CPU limits
        constraints_checked.append("nodepool_cpu")
        total_pool_cpu = sum(p.cpu_limit for p in pools)
        if total_cpu_cores > total_pool_cpu:
            blocking.append(f"Workloads need {total_cpu_cores:.0f} vCPU, NodePool limits total {total_pool_cpu}")
            recommendations.append("Increase NodePool CPU limits")
        else:
            log.info("  NodePool CPU: %d limit, %.0f needed — OK", total_pool_cpu, total_cpu_cores)

        # 4. Pod ceiling — check per pool, matching workload affinity
        constraints_checked.append("pod_ceiling")

        # Determine which pool(s) the workloads target based on nodeSelector/affinity
        target_pools = set()
        for d in deployments:
            # Check nodeSelector for karpenter.sh/nodepool
            pool_label = d.node_selector.get("karpenter.sh/nodepool", "")
            if pool_label:
                target_pools.add(pool_label)
            # Check nodeAffinity for karpenter label
            affinity = d.affinity
            if isinstance(affinity, dict):
                na = affinity.get("nodeAffinity", {})
                req = na.get("requiredDuringSchedulingIgnoredDuringExecution", {})
                for term in req.get("nodeSelectorTerms", []):
                    for expr in term.get("matchExpressions", []):
                        if expr.get("key") == "karpenter" and expr.get("operator") == "In":
                            for v in expr.get("values", []):
                                target_pools.add(v)

        pool_ceilings = {}
        for pool in pools:
            nc = nc_by_name.get(pool.node_class_name)
            max_pods_per_node = nc.max_pods if nc else 110
            usable_pods = max(max_pods_per_node - daemonset_count, 0)
            min_vcpu = min((INSTANCE_TYPE_CORES.get(t, 32) for t in pool.instance_types), default=32)
            max_nodes = pool.cpu_limit // min_vcpu
            pool_ceiling = max_nodes * usable_pods
            pool_ceilings[pool.name] = pool_ceiling
            if not target_pools or pool.name in target_pools:
                log.info("  Pool %s: %d nodes × %d usable pods/node (%d maxPods - %d daemonsets) = %d ceiling",
                         pool.name, max_nodes, usable_pods, max_pods_per_node, daemonset_count, pool_ceiling)

        # If workloads target specific pools, only count those pools
        if target_pools:
            relevant_ceiling = sum(pool_ceilings.get(p, 0) for p in target_pools)
            log.info("  Workloads target pools: %s → relevant ceiling: %d",
                     ", ".join(target_pools), relevant_ceiling)
        else:
            relevant_ceiling = sum(pool_ceilings.values())

        # Apply scheduling efficiency factor — topology spread, AZ imbalance,
        # and DaemonSet churn mean not all slots are usable in practice
        scheduling_efficiency = 0.90
        effective_ceiling = int(relevant_ceiling * scheduling_efficiency)

        if self.config.target_pods > effective_ceiling:
            shortfall = self.config.target_pods - effective_ceiling
            pools_str = ", ".join(target_pools) if target_pools else "all"
            blocking.append(
                f"Target {self.config.target_pods} exceeds effective pod ceiling {effective_ceiling} "
                f"on pools [{pools_str}] "
                f"(raw ceiling {relevant_ceiling} × {scheduling_efficiency:.0%} efficiency, "
                f"shortfall: {shortfall})")
            recommendations.append("Reduce target, increase NodePool CPU limits or maxPods")
        else:
            log.info("  Pod ceiling: %d effective (%d raw × %.0f%%), target %d — OK",
                     effective_ceiling, relevant_ceiling, scheduling_efficiency * 100, self.config.target_pods)

        # 5. Observability connectivity — non-blocking but critical for investigation
        constraints_checked.append("observability")
        obs_ok = 0
        obs_total = 0

        # 5a. AMP / Prometheus
        obs_total += 1
        if self.config.amp_workspace_id or getattr(self.config, 'prometheus_url', None):
            try:
                from k8s_scale_test.health_sweep import AMPMetricCollector
                collector = AMPMetricCollector(
                    amp_workspace_id=self.config.amp_workspace_id,
                    prometheus_url=getattr(self.config, 'prometheus_url', None),
                    aws_profile=self.config.aws_profile,
                )
                result = await collector._query_promql("up")
                if result.get("status") == "success":
                    log.info("  AMP connectivity: OK")
                    obs_ok += 1
                else:
                    recommendations.append(f"AMP returned status '{result.get('status')}' — metrics unavailable")
                    log.warning("  AMP connectivity: unexpected status '%s'", result.get("status"))
            except Exception as exc:
                recommendations.append(f"AMP connectivity failed: {exc}")
                log.warning("  AMP connectivity: FAILED — %s", exc)
        else:
            recommendations.append("No AMP workspace or Prometheus URL configured")
            log.info("  AMP connectivity: skipped (not configured)")

        # 5b. CloudWatch Logs
        obs_total += 1
        if self.config.cloudwatch_log_group:
            try:
                cw = self.aws_client.client("logs", region_name="us-west-2")
                resp = cw.describe_log_groups(
                    logGroupNamePrefix=self.config.cloudwatch_log_group,
                    limit=1,
                )
                groups = resp.get("logGroups", [])
                if groups and groups[0].get("logGroupName") == self.config.cloudwatch_log_group:
                    log.info("  CloudWatch Logs: OK (log group exists)")
                    obs_ok += 1
                else:
                    recommendations.append(
                        f"CloudWatch log group '{self.config.cloudwatch_log_group}' not found"
                    )
                    log.warning("  CloudWatch Logs: log group not found")
            except Exception as exc:
                recommendations.append(f"CloudWatch Logs connectivity failed: {exc}")
                log.warning("  CloudWatch Logs: FAILED — %s", exc)
        else:
            recommendations.append("No CloudWatch log group configured")
            log.info("  CloudWatch Logs: skipped (not configured)")

        # 5c. EKS API (already implicitly tested by earlier K8s calls, but verify cluster name)
        obs_total += 1
        if self.config.eks_cluster_name:
            try:
                eks = self.aws_client.client("eks", region_name="us-west-2")
                cluster = eks.describe_cluster(name=self.config.eks_cluster_name)
                status = cluster.get("cluster", {}).get("status", "")
                if status == "ACTIVE":
                    log.info("  EKS API: OK (cluster '%s' is ACTIVE)", self.config.eks_cluster_name)
                    obs_ok += 1
                else:
                    recommendations.append(
                        f"EKS cluster '{self.config.eks_cluster_name}' status is '{status}', not ACTIVE"
                    )
                    log.warning("  EKS API: cluster status '%s'", status)
            except Exception as exc:
                recommendations.append(f"EKS API connectivity failed: {exc}")
                log.warning("  EKS API: FAILED — %s", exc)
        else:
            recommendations.append("No EKS cluster name configured")
            log.info("  EKS API: skipped (not configured)")

        if obs_ok == obs_total:
            log.info("  Observability: all %d checks passed", obs_total)
        else:
            log.warning("  Observability: %d/%d checks passed — investigation capability degraded",
                        obs_ok, obs_total)

        # Max achievable
        avg_cpu_per_pod = total_cpu_m / self.config.target_pods if self.config.target_pods > 0 else 1
        max_by_cpu = int(total_pool_cpu * 1000 / avg_cpu_per_pod) if avg_cpu_per_pod > 0 else 0
        max_achievable = min(total_ips, max_by_cpu, effective_ceiling)

        decision = GoNoGoDecision(
            decision=GoNoGo.NO_GO if blocking else GoNoGo.GO,
            constraints_checked=constraints_checked,
            blocking_constraints=blocking,
            recommendations=recommendations,
        )

        report = PreflightReport(
            timestamp=datetime.now(timezone.utc),
            config=self.config,
            ec2_quotas=ec2_quotas,
            subnet_ips=subnet_ips,
            total_available_ips=total_ips,
            nodepool_capacities=[NodePoolCapacity(
                name=p.name, instance_types=p.instance_types,
                vcpus_per_instance={}, max_nodes_per_type={},
                max_pods_per_type={}, total_max_pods=0,
            ) for p in pools],
            pods_per_node_breakdowns=pods_per_node_bds,
            pod_sizing_recommendations=pod_sizing_recs,
            max_achievable_pods=max_achievable,
            decision=decision,
            stressor_sizing=stressor_sizing,
        )

        log.info("Preflight: max_achievable=%d, target=%d, decision=%s",
                 max_achievable, self.config.target_pods, decision.decision.value)
        if blocking:
            for b in blocking:
                log.warning("  BLOCKING: %s", b)
        for r in recommendations:
            log.info("  Recommendation: %s", r)
        return report
    async def _count_daemonsets(self) -> int:
        """Count DaemonSets that run on every node (they consume pod slots)."""
        try:
            v1 = self.k8s_client.AppsV1Api()
            ds_list = v1.list_daemon_set_for_all_namespaces(watch=False)
            count = 0
            for ds in ds_list.items:
                desired = ds.status.desired_number_scheduled or 0
                if desired > 0:
                    count += 1
            return count
        except Exception as exc:
            logging.getLogger(__name__).warning("DaemonSet count failed: %s", exc)
            return 10  # Conservative default


