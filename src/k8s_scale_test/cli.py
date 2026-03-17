"""CLI entry point for the Kubernetes scale testing system."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from k8s_scale_test.controller import ScaleTestController
from k8s_scale_test.evidence import EvidenceStore
from k8s_scale_test.models import TestConfig


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="k8s-scale-test",
        description="Kubernetes performance and scale testing system",
    )
    p.add_argument("--target-pods", type=int, required=True, help="Target pod count")
    p.add_argument("--pending-timeout", type=float, default=600.0, help="Seconds before timeout")
    p.add_argument("--kubeconfig", type=str, default=None)
    p.add_argument("--aws-profile", type=str, default=None)
    p.add_argument("--flux-repo-path", type=str, default="/Users/shancor/Projects/flux/flux2")
    p.add_argument("--output-dir", type=str, default="./scale-test-results")
    p.add_argument("--prometheus-url", type=str, default=None)
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--auto-approve", action="store_true", help="Auto-approve preflight and operator prompts")
    p.add_argument("--exclude-apps", type=str, default="", help="Comma-separated app names to exclude from scaling")
    p.add_argument("--include-apps", type=str, default="", help="Comma-separated app names to include (only these will scale)")
    p.add_argument("--cl2-preload", type=str, default="mixed-workload",
                   help="CL2 config template name for preload phase (default: mixed-workload)")
    p.add_argument("--cl2-timeout", type=float, default=3600.0,
                   help="CL2 Job timeout in seconds (default: 3600)")
    p.add_argument("--cl2-params", type=str, default=None,
                   help="Comma-separated CL2 template params (e.g., REPLICAS=100,SERVICES=50)")
    p.add_argument("--hold-at-peak", type=int, default=90,
                   help="Seconds to hold at peak pod count before cleanup (default: 90)")
    p.add_argument("--stressor-weights", type=str, default=None,
                   help='JSON dict of deployment name to weight, e.g. \'{"cpu-stress-test": 0.4, "memory-stress-test": 0.3, "io-stress-test": 0.2, "iperf3-client": 0.1}\'')
    p.add_argument("--cpu-limit-multiplier", type=float, default=2.0,
                   help="CPU limit as multiplier of request (default: 2.0)")
    p.add_argument("--memory-limit-multiplier", type=float, default=1.5,
                   help="Memory limit as multiplier of request (default: 1.5)")
    p.add_argument("--iperf3-server-ratio", type=int, default=50,
                   help="Number of client pods per iperf3 server (default: 50)")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    if args.verbose:
        logging.getLogger("k8s_scale_test").setLevel(logging.DEBUG)
    # Keep boto3/botocore/urllib3 quiet
    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("boto3").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("kubernetes").setLevel(logging.WARNING)

    cl2_params = None
    if args.cl2_params:
        cl2_params = {}
        for pair in args.cl2_params.split(","):
            if "=" in pair:
                k, v = pair.split("=", 1)
                cl2_params[k.strip()] = v.strip()

    stressor_weights = None
    if args.stressor_weights:
        stressor_weights = json.loads(args.stressor_weights)

    config = TestConfig(
        target_pods=args.target_pods,
        pending_timeout_seconds=args.pending_timeout,
        kubeconfig=args.kubeconfig,
        aws_profile=args.aws_profile,
        flux_repo_path=args.flux_repo_path,
        output_dir=args.output_dir,
        prometheus_url=args.prometheus_url,
        auto_approve=args.auto_approve,
        exclude_apps=[x.strip() for x in args.exclude_apps.split(",") if x.strip()] if args.exclude_apps else [],
        include_apps=[x.strip() for x in args.include_apps.split(",") if x.strip()] if args.include_apps else [],
        cl2_preload=args.cl2_preload,
        cl2_timeout=args.cl2_timeout,
        cl2_params=cl2_params,
        hold_at_peak=args.hold_at_peak,
        stressor_weights=stressor_weights,
        cpu_limit_multiplier=args.cpu_limit_multiplier,
        memory_limit_multiplier=args.memory_limit_multiplier,
        iperf3_server_ratio=args.iperf3_server_ratio,
    )

    evidence_store = EvidenceStore(config.output_dir)

    # Initialize K8s client
    try:
        import kubernetes
        if config.kubeconfig:
            kubernetes.config.load_kube_config(config_file=config.kubeconfig)
        else:
            kubernetes.config.load_kube_config()
        k8s_client = kubernetes.client

        # Patch the K8s config to auto-refresh tokens by reloading kubeconfig
        # before each API call batch. Store the loader for the controller to use.
        config._k8s_reload = lambda: (
            kubernetes.config.load_kube_config(config_file=config.kubeconfig)
            if config.kubeconfig
            else kubernetes.config.load_kube_config()
        )
    except Exception as exc:
        logging.error("Failed to initialize K8s client: %s", exc)
        sys.exit(1)

    # Initialize AWS client — export credentials from AWS CLI session
    # so boto3 can find them (aws login stores creds in SSO cache,
    # not in a format boto3 resolves natively)
    try:
        import boto3
        import os
        import subprocess

        # Try to export credentials from the AWS CLI session
        _export = subprocess.run(
            ["aws", "configure", "export-credentials", "--format", "env"],
            capture_output=True, text=True, timeout=10,
        )
        if _export.returncode == 0:
            for line in _export.stdout.strip().splitlines():
                line = line.replace("export ", "")
                if "=" in line:
                    k, v = line.split("=", 1)
                    os.environ[k] = v

        session_kwargs = {}
        if config.aws_profile:
            session_kwargs["profile_name"] = config.aws_profile
        aws_session = boto3.Session(**session_kwargs)
        aws_client = aws_session
    except Exception as exc:
        logging.error("Failed to initialize AWS client: %s", exc)
        sys.exit(1)

    controller = ScaleTestController(config, evidence_store, k8s_client, aws_client)

    try:
        summary = asyncio.run(controller.run())
        _print_report(summary, config)
    except KeyboardInterrupt:
        print("\nTest interrupted by user. Partial results saved.")
        sys.exit(130)


def _print_report(summary, config):
    from k8s_scale_test.models import RunValidity
    target = config.target_pods
    peak = summary.peak_pod_count
    shortfall = target - peak

    print(f"\n{'='*70}")
    if summary.validity == RunValidity.VALID:
        print(f"  PASSED — {peak:,} pods reached target {target:,}")
    else:
        print(f"  FAILED — {peak:,}/{target:,} pods ({shortfall:,} shortfall)")
        print(f"  Reason: {summary.validity_reason}")
    print(f"{'='*70}")

    # Steps table
    print(f"\n  {'Step':>4} {'Target':>8} {'Ready':>8} {'Pending':>8} {'Cumul Time':>11} {'Rate':>8}")
    print(f"  {'-'*55}")
    prev = 0
    cumulative_sec = 0
    for s in summary.scaling_result.steps:
        delta = s.actual_ready - prev
        rate = delta / s.duration_seconds if s.duration_seconds > 0 else 0
        cumulative_sec += s.duration_seconds
        cumul_min = cumulative_sec / 60
        print(f"  {s.step_number:>4} {s.target_replicas:>8,} {s.actual_ready:>8,} "
              f"{s.actual_pending:>8,} {cumul_min:>9.1f}m {rate:>7.1f}/s")
        prev = s.actual_ready

    # Failure analysis — quantify each problem
    if summary.validity != RunValidity.VALID and summary.findings:
        print(f"\n  FAILURE ANALYSIS ({shortfall:,} pods not reached)")
        print(f"  {'-'*52}")

        # Aggregate across all findings
        ipamd_zero = 0
        iam_auth_nodes = set()
        sandbox_failures = 0
        scheduling_failures = 0
        stuck_creating = 0
        unscheduled = 0

        for f in summary.findings:
            rc = f.root_cause or ""
            # Count IPAMD zero-prefix
            for ref in f.evidence_references:
                if "zero_prefix=" in ref:
                    try:
                        n = int(ref.split("zero_prefix=")[1].split(",")[0].split(")")[0])
                        ipamd_zero = max(ipamd_zero, n)
                    except Exception:
                        pass
                if "FailedCreatePodSandBox=" in ref:
                    try:
                        n = int(ref.split("FailedCreatePodSandBox=")[1].split(",")[0])
                        sandbox_failures = max(sandbox_failures, n)
                    except Exception:
                        pass
                if "FailedScheduling=" in ref:
                    try:
                        n = int(ref.split("FailedScheduling=")[1].split(",")[0])
                        scheduling_failures = max(scheduling_failures, n)
                    except Exception:
                        pass

            # Count IAM auth nodes
            for part in rc.split(";"):
                if "IAM auth failure" in part:
                    # Extract node name
                    node = part.split("on ")[-1].strip() if "on " in part else ""
                    if node:
                        iam_auth_nodes.add(node[:50])
                if "IPAMD failure" in part or "0 prefixes" in part:
                    pass  # Already counted via evidence refs
                if "truly unscheduled" in part:
                    try:
                        n = int(part.split()[0])
                        unscheduled = max(unscheduled, n)
                    except Exception:
                        pass
                if "stuck in ContainerCreating" in part:
                    try:
                        n = int(part.split()[0])
                        stuck_creating = max(stuck_creating, n)
                    except Exception:
                        pass

        if ipamd_zero > 0:
            print(f"    IPAMD zero-prefix: {ipamd_zero} nodes had 0 IPv4 prefixes (IPAMD init failure)")
        if iam_auth_nodes:
            print(f"    IAM auth failures: {len(iam_auth_nodes)} nodes could not call EC2 APIs")
        if sandbox_failures > 0:
            print(f"    CNI sandbox failures: {sandbox_failures:,} FailedCreatePodSandBox events")
        if scheduling_failures > 0:
            print(f"    Scheduling failures: {scheduling_failures:,} FailedScheduling events")
        if stuck_creating > 0:
            print(f"    ContainerCreating: {stuck_creating:,} pods scheduled but waiting on CNI/image")
        if unscheduled > 0:
            print(f"    Unscheduled: {unscheduled:,} pods with no node assigned")
        if not any([ipamd_zero, iam_auth_nodes, sandbox_failures, stuck_creating, unscheduled]):
            print("    No specific root cause identified — check findings for details")

    # Summary line
    dur_min = summary.duration_seconds / 60
    print(f"\n  Duration: {dur_min:.1f}m | "
          f"Nodes: {summary.total_nodes_provisioned} | "
          f"Anomalies: {summary.anomaly_count}")
    print(f"  Results: {config.output_dir}/{summary.run_id}/")
    print(f"  Chart: {config.output_dir}/{summary.run_id}/chart.html")


if __name__ == "__main__":
    main()
