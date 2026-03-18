"""CLI entry point for the Kubernetes scale testing system."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from k8s_scale_test.models import TestConfig

log = logging.getLogger(__name__)


def _add_run_args(p: argparse.ArgumentParser) -> None:
    """Add the scale-test run arguments to *p*."""
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
    p.add_argument("--amp-workspace-id", type=str, default=None,
                   help="AMP workspace ID for Prometheus MCP server")
    p.add_argument("--cloudwatch-log-group", type=str, default=None,
                   help="CloudWatch log group for node logs")
    p.add_argument("--eks-cluster-name", type=str, default=None,
                   help="EKS cluster name for EKS MCP server")
    p.add_argument("--enable-tracing", action="store_true",
                   help="Enable OpenTelemetry tracing (exports to X-Ray)")


def _add_kb_subcommands(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``kb`` subcommand group."""
    kb_parser = subparsers.add_parser("kb", help="Known Issues Knowledge Base commands")
    kb_sub = kb_parser.add_subparsers(dest="kb_command")

    # Shared optional args for all kb commands
    for name, sp in [
        ("setup", kb_sub.add_parser("setup", help="Create DynamoDB table and verify S3 bucket")),
        ("list", kb_sub.add_parser("list", help="List all KB entries")),
        ("search", kb_sub.add_parser("search", help="Search KB entries by text query")),
        ("show", kb_sub.add_parser("show", help="Show full details of a KB entry")),
        ("add", kb_sub.add_parser("add", help="Add a new KB entry")),
        ("remove", kb_sub.add_parser("remove", help="Remove a KB entry")),
        ("approve", kb_sub.add_parser("approve", help="Approve a pending KB entry")),
        ("ingest", kb_sub.add_parser("ingest", help="Ingest findings from a run directory")),
        ("seed", kb_sub.add_parser("seed", help="Populate KB with seed entries")),
    ]:
        sp.add_argument("--aws-profile", type=str, default=None, help="AWS profile name")
        sp.add_argument("--kb-table", type=str, default="scale-test-kb", help="DynamoDB table name")
        sp.add_argument("--kb-bucket", type=str, default=None, help="S3 bucket for KB entries")
        sp.add_argument("--kb-prefix", type=str, default="kb-entries/", help="S3 key prefix")

    # list-specific
    list_p = kb_sub.choices["list"]
    list_p.add_argument("--pending", action="store_true", help="Show only pending entries")

    # search-specific
    search_p = kb_sub.choices["search"]
    search_p.add_argument("query", help="Text to search for in title and root cause")

    # show-specific
    show_p = kb_sub.choices["show"]
    show_p.add_argument("entry_id", help="KB entry identifier")

    # add-specific
    add_p = kb_sub.choices["add"]
    add_p.add_argument("--title", required=True, help="Entry title")
    add_p.add_argument("--category", required=True,
                       choices=["networking", "scheduling", "capacity", "runtime", "storage", "control-plane"],
                       help="Entry category")
    add_p.add_argument("--root-cause", required=True, help="Root cause description")
    add_p.add_argument("--event-reasons", required=True, help="Comma-separated K8s event reasons")
    add_p.add_argument("--log-patterns", type=str, default=None, help="Comma-separated log regex patterns")
    add_p.add_argument("--severity", type=str, default="warning",
                       choices=["info", "warning", "critical"], help="Severity level (default: warning)")
    add_p.add_argument("--component", type=str, default=None, help="Affected component name")
    add_p.add_argument("--min-version", type=str, default=None, help="Minimum affected version")
    add_p.add_argument("--max-version", type=str, default=None, help="Maximum affected version")
    add_p.add_argument("--fixed-in", type=str, default=None, help="Version where issue was fixed")

    # remove-specific
    remove_p = kb_sub.choices["remove"]
    remove_p.add_argument("entry_id", help="KB entry identifier to remove")

    # approve-specific
    approve_p = kb_sub.choices["approve"]
    approve_p.add_argument("entry_id", help="KB entry identifier to approve")

    # ingest-specific
    ingest_p = kb_sub.choices["ingest"]
    ingest_p.add_argument("run_dir", help="Path to the run directory containing findings")

    # seed has no extra args


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="k8s-scale-test",
        description="Kubernetes performance and scale testing system",
    )
    subparsers = p.add_subparsers(dest="command")

    # ``run`` subcommand — the original scale-test behaviour
    run_parser = subparsers.add_parser("run", help="Run a scale test")
    _add_run_args(run_parser)

    # ``kb`` subcommand group
    _add_kb_subcommands(subparsers)

    # Backwards compat: if no subcommand given but --target-pods is present,
    # treat as a ``run`` invocation (legacy mode).
    if argv is None:
        raw = sys.argv[1:]
    else:
        raw = list(argv)

    if raw and raw[0] not in ("run", "kb", "-h", "--help"):
        # Legacy invocation — prepend "run"
        raw = ["run"] + raw

    return p.parse_args(raw)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)

    if args.command == "kb":
        _handle_kb(args)
        return

    # ``run`` command (or legacy invocation)

    _setup_logging(getattr(args, "verbose", False))

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
        amp_workspace_id=args.amp_workspace_id,
        cloudwatch_log_group=args.cloudwatch_log_group,
        eks_cluster_name=args.eks_cluster_name,
        enable_tracing=args.enable_tracing,
    )

    from k8s_scale_test.controller import ScaleTestController
    from k8s_scale_test.evidence import EvidenceStore

    evidence_store = EvidenceStore(config.output_dir)

    # Initialize K8s client
    try:
        import kubernetes
        if config.kubeconfig:
            kubernetes.config.load_kube_config(config_file=config.kubeconfig)
        else:
            kubernetes.config.load_kube_config()
        k8s_client = kubernetes.client

        config._k8s_reload = lambda: (
            kubernetes.config.load_kube_config(config_file=config.kubeconfig)
            if config.kubeconfig
            else kubernetes.config.load_kube_config()
        )
    except Exception as exc:
        logging.error("Failed to initialize K8s client: %s", exc)
        sys.exit(1)

    try:
        aws_client = _make_aws_session(config.aws_profile)
    except Exception as exc:
        logging.error("Failed to initialize AWS client: %s", exc)
        sys.exit(1)

    if config.enable_tracing:
        from k8s_scale_test.tracing import init_tracing
        if not init_tracing(aws_client):
            log.warning("Tracing initialization failed, continuing without tracing")

    controller = ScaleTestController(config, evidence_store, k8s_client, aws_client)

    try:
        summary = asyncio.run(controller.run())
        _print_report(summary, config)
        if config.enable_tracing:
            from k8s_scale_test.tracing import shutdown, get_trace_url
            shutdown()
            print(f"  X-Ray trace: {get_trace_url(summary.run_id)}")
    except KeyboardInterrupt:
        print("\nTest interrupted by user. Partial results saved.")
        if config.enable_tracing:
            from k8s_scale_test.tracing import shutdown
            shutdown()
        sys.exit(130)


def _setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    if verbose:
        logging.getLogger("k8s_scale_test").setLevel(logging.DEBUG)
    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("boto3").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("kubernetes").setLevel(logging.WARNING)


def _make_aws_session(profile: str | None = None):
    """Create a boto3 session, exporting credentials from the AWS CLI if needed."""
    import boto3
    import subprocess

    try:
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
    except Exception:
        pass

    session_kwargs = {}
    if profile:
        session_kwargs["profile_name"] = profile
    return boto3.Session(**session_kwargs)


# ------------------------------------------------------------------
# KB subcommand handlers
# ------------------------------------------------------------------


def _handle_kb(args: argparse.Namespace) -> None:
    """Dispatch to the appropriate ``kb`` subcommand handler."""
    _setup_logging()

    cmd = getattr(args, "kb_command", None)
    if cmd is None:
        print("Usage: k8s-scale-test kb <command>")
        print("Commands: setup, list, search, show, add, remove, approve, ingest, seed")
        sys.exit(1)

    handlers = {
        "setup": _kb_setup,
        "list": _kb_list,
        "search": _kb_search,
        "show": _kb_show,
        "add": _kb_add,
        "remove": _kb_remove,
        "approve": _kb_approve,
        "ingest": _kb_ingest,
        "seed": _kb_seed,
    }
    handler = handlers.get(cmd)
    if handler is None:
        print(f"Unknown kb command: {cmd}")
        sys.exit(1)
    handler(args)


def _make_kb_store(args: argparse.Namespace):
    """Create a KBStore from CLI args (direct DynamoDB/S3, no local cache used)."""
    from k8s_scale_test.kb_store import KBStore

    aws_session = _make_aws_session(getattr(args, "aws_profile", None))
    return KBStore(
        aws_client=aws_session,
        table_name=getattr(args, "kb_table", "scale-test-kb"),
        s3_bucket=getattr(args, "kb_bucket", "") or "",
        s3_prefix=getattr(args, "kb_prefix", "kb-entries/"),
    )


def _kb_setup(args: argparse.Namespace) -> None:
    """``kb setup`` — create DynamoDB table and verify S3 bucket."""
    from k8s_scale_test.kb_store import KBStore

    aws_session = _make_aws_session(getattr(args, "aws_profile", None))
    table_name = getattr(args, "kb_table", "scale-test-kb")
    bucket = getattr(args, "kb_bucket", None)

    KBStore.setup_table(aws_session, table_name)
    print(f"DynamoDB table '{table_name}' is ready.")

    if not bucket:
        print("No S3 bucket specified (--kb-bucket). Skipping bucket verification.")
        return

    if not KBStore.verify_bucket(aws_session, bucket):
        print(f"ERROR: S3 bucket '{bucket}' does not exist or versioning is not enabled.")
        print("Please create the bucket manually with versioning enabled.")
        sys.exit(1)

    print(f"S3 bucket '{bucket}' verified (exists, versioning enabled).")


def _kb_list(args: argparse.Namespace) -> None:
    """``kb list`` — display all entries as a table."""
    store = _make_kb_store(args)
    entries = store.load_all_direct()

    if getattr(args, "pending", False):
        entries = [e for e in entries if e.status == "pending"]

    if not entries:
        print("No KB entries found.")
        return

    # Table header
    fmt = "{:<30s} {:<45s} {:<15s} {:<10s} {:<10s} {:>6s}"
    print(fmt.format("ID", "TITLE", "CATEGORY", "SEVERITY", "STATUS", "COUNT"))
    print("-" * 120)
    for e in sorted(entries, key=lambda x: x.entry_id):
        sev = e.severity.value if hasattr(e.severity, "value") else str(e.severity)
        title = e.title[:44] + "…" if len(e.title) > 45 else e.title
        print(fmt.format(
            e.entry_id[:30],
            title,
            e.category[:15],
            sev[:10],
            e.status[:10],
            str(e.occurrence_count),
        ))


def _kb_search(args: argparse.Namespace) -> None:
    """``kb search <query>`` — text search via direct DynamoDB scan + filter."""
    store = _make_kb_store(args)
    query = args.query.lower()
    entries = store.load_all_direct()
    results = [
        e for e in entries
        if query in e.title.lower() or query in e.root_cause.lower()
    ]

    if not results:
        print(f"No entries matching '{args.query}'.")
        return

    fmt = "{:<30s} {:<45s} {:<15s} {:<10s} {:<10s}"
    print(fmt.format("ID", "TITLE", "CATEGORY", "SEVERITY", "STATUS"))
    print("-" * 112)
    for e in results:
        sev = e.severity.value if hasattr(e.severity, "value") else str(e.severity)
        title = e.title[:44] + "…" if len(e.title) > 45 else e.title
        print(fmt.format(e.entry_id[:30], title, e.category[:15], sev[:10], e.status[:10]))


def _kb_show(args: argparse.Namespace) -> None:
    """``kb show <entry_id>`` — display full entry details."""
    store = _make_kb_store(args)
    entry = store.get_direct(args.entry_id)
    if entry is None:
        print(f"ERROR: Entry '{args.entry_id}' not found.")
        sys.exit(1)

    sev = entry.severity.value if hasattr(entry.severity, "value") else str(entry.severity)
    print(f"Entry ID:    {entry.entry_id}")
    print(f"Title:       {entry.title}")
    print(f"Category:    {entry.category}")
    print(f"Severity:    {sev}")
    print(f"Status:      {entry.status}")
    print(f"Occurrences: {entry.occurrence_count}")
    print(f"Created:     {entry.created_at}")
    print(f"Last Seen:   {entry.last_seen}")
    print(f"\nRoot Cause:\n  {entry.root_cause}")
    print(f"\nRecommended Actions:")
    for i, action in enumerate(entry.recommended_actions, 1):
        print(f"  {i}. {action}")

    # Signature
    sig = entry.signature
    print(f"\nSignature:")
    if sig.event_reasons:
        print(f"  Event Reasons: {', '.join(sig.event_reasons)}")
    if sig.log_patterns:
        print(f"  Log Patterns:  {', '.join(sig.log_patterns)}")
    if sig.metric_conditions:
        print(f"  Metric Conds:  {', '.join(sig.metric_conditions)}")
    if sig.resource_kinds:
        print(f"  Resource Kinds: {', '.join(sig.resource_kinds)}")

    # Affected versions
    if entry.affected_versions:
        print(f"\nAffected Versions:")
        for av in entry.affected_versions:
            parts = [f"  {av.component}"]
            if av.min_version:
                parts.append(f"min={av.min_version}")
            if av.max_version:
                parts.append(f"max={av.max_version}")
            if av.fixed_in:
                parts.append(f"fixed_in={av.fixed_in}")
            print(" ".join(parts))

    # Optional fields
    if entry.review_notes:
        print(f"\nReview Notes:\n  {entry.review_notes}")
    if entry.alternative_explanations:
        print(f"\nAlternative Explanations:")
        for alt in entry.alternative_explanations:
            print(f"  - {alt}")
    if entry.checkpoint_questions:
        print(f"\nCheckpoint Questions:")
        for q in entry.checkpoint_questions:
            print(f"  - {q}")


def _kb_add(args: argparse.Namespace) -> None:
    """``kb add`` — create a new KB entry."""
    from k8s_scale_test.models import AffectedVersions, KBEntry, Severity, Signature

    store = _make_kb_store(args)
    now = datetime.now(timezone.utc)

    # Generate entry_id from title slug
    slug = re.sub(r"[^a-z0-9]+", "-", args.title.lower()).strip("-")[:60]
    entry_id = slug

    event_reasons = [r.strip() for r in args.event_reasons.split(",") if r.strip()]
    log_patterns = []
    if args.log_patterns:
        log_patterns = [p.strip() for p in args.log_patterns.split(",") if p.strip()]

    severity = Severity(args.severity)

    affected_versions = []
    if args.component:
        affected_versions.append(AffectedVersions(
            component=args.component,
            min_version=args.min_version,
            max_version=args.max_version,
            fixed_in=args.fixed_in,
        ))

    entry = KBEntry(
        entry_id=entry_id,
        title=args.title,
        category=args.category,
        signature=Signature(
            event_reasons=event_reasons,
            log_patterns=log_patterns,
            metric_conditions=[],
            resource_kinds=[],
        ),
        root_cause=args.root_cause,
        recommended_actions=[],
        severity=severity,
        affected_versions=affected_versions,
        created_at=now,
        last_seen=now,
        occurrence_count=0,
        status="active",
    )

    store.save(entry)
    print(f"Created KB entry: {entry_id}")


def _kb_remove(args: argparse.Namespace) -> None:
    """``kb remove <entry_id>`` — delete an entry."""
    store = _make_kb_store(args)

    # Check if entry exists first
    entry = store.get_direct(args.entry_id)
    if entry is None:
        print(f"ERROR: Entry '{args.entry_id}' not found.")
        sys.exit(1)

    store.delete(args.entry_id)
    print(f"Removed KB entry: {args.entry_id}")


def _kb_approve(args: argparse.Namespace) -> None:
    """``kb approve <entry_id>`` — change status from pending to active."""
    store = _make_kb_store(args)
    entry = store.get_direct(args.entry_id)
    if entry is None:
        print(f"ERROR: Entry '{args.entry_id}' not found.")
        sys.exit(1)

    if entry.status == "active":
        print(f"Entry '{args.entry_id}' is already active.")
        return

    entry.status = "active"
    store.save(entry)
    print(f"Approved KB entry: {args.entry_id} (pending → active)")


def _kb_ingest(args: argparse.Namespace) -> None:
    """``kb ingest <run_dir>`` — process resolved findings."""
    from k8s_scale_test.kb_ingest import IngestionPipeline
    from k8s_scale_test.kb_matcher import SignatureMatcher

    run_dir = args.run_dir
    if not Path(run_dir).is_dir():
        print(f"ERROR: Run directory '{run_dir}' does not exist.")
        sys.exit(1)

    store = _make_kb_store(args)
    matcher = SignatureMatcher()
    pipeline = IngestionPipeline(store, matcher)
    results = pipeline.ingest_run(run_dir)
    print(f"Ingested {len(results)} entries from {run_dir}")


def _kb_seed(args: argparse.Namespace) -> None:
    """``kb seed`` — populate KB with seed entries."""
    from k8s_scale_test.kb_seed import load_seed_entries

    store = _make_kb_store(args)
    entries = load_seed_entries(store)
    print(f"Seeded {len(entries)} KB entries.")


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
    if summary.findings:
        if summary.validity != RunValidity.VALID:
            print(f"\n  FAILURE ANALYSIS ({shortfall:,} pods not reached)")
        else:
            print(f"\n  ANOMALIES ({len(summary.findings)} finding(s) during run)")
        print(f"  {'-'*52}")

        # Aggregate across all findings
        ipamd_zero = 0
        iam_auth_nodes = set()
        sandbox_failures = 0
        scheduling_failures = 0
        stuck_creating = 0
        unscheduled = 0
        image_pull_failures = 0
        insufficient_capacity = 0
        webhook_failures = 0
        invalid_disk = 0

        for f in summary.findings:
            rc = f.root_cause or ""
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
                if "InvalidDiskCapacity=" in ref:
                    try:
                        n = int(ref.split("InvalidDiskCapacity=")[1].split(",")[0])
                        invalid_disk = max(invalid_disk, n)
                    except Exception:
                        pass
                if "InsufficientCapacityError=" in ref:
                    try:
                        n = int(ref.split("InsufficientCapacityError=")[1].split(",")[0])
                        insufficient_capacity = max(insufficient_capacity, n)
                    except Exception:
                        pass

            # Count from k8s_events
            if hasattr(f, 'k8s_events') and f.k8s_events:
                for ev in f.k8s_events:
                    msg = ev.message or ""
                    reason = ev.reason or ""
                    if "pull QPS exceeded" in msg or reason == "Failed" and "ImagePull" in msg:
                        image_pull_failures += ev.count if hasattr(ev, 'count') and ev.count else 1
                    if "kyverno" in msg.lower() and reason == "FailedCreate":
                        webhook_failures += ev.count if hasattr(ev, 'count') and ev.count else 1

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
        if insufficient_capacity > 0:
            print(f"    InsufficientCapacity: {insufficient_capacity} EC2 CreateFleet failures (AZ exhaustion)")
        if sandbox_failures > 0:
            print(f"    CNI sandbox failures: {sandbox_failures:,} FailedCreatePodSandBox events")
        if scheduling_failures > 0:
            print(f"    Scheduling failures: {scheduling_failures:,} FailedScheduling events")
        if image_pull_failures > 0:
            print(f"    Image pull failures: {image_pull_failures:,} pull QPS exceeded events")
        if webhook_failures > 0:
            print(f"    Webhook failures: {webhook_failures:,} Kyverno webhook errors (pod creation blocked)")
        if invalid_disk > 0:
            print(f"    InvalidDiskCapacity: {invalid_disk} nodes reported 0 image filesystem capacity")
        if stuck_creating > 0:
            print(f"    ContainerCreating: {stuck_creating:,} pods scheduled but waiting on CNI/image")
        if unscheduled > 0:
            print(f"    Unscheduled: {unscheduled:,} pods with no node assigned")
        if not any([ipamd_zero, iam_auth_nodes, sandbox_failures, stuck_creating,
                     unscheduled, image_pull_failures, insufficient_capacity,
                     webhook_failures, invalid_disk]):
            # Show root causes from findings directly
            for f in summary.findings:
                if f.root_cause:
                    print(f"    {f.severity}: {f.root_cause}")

    # Summary line
    dur_min = summary.duration_seconds / 60
    print(f"\n  Duration: {dur_min:.1f}m | "
          f"Nodes: {summary.total_nodes_provisioned} | "
          f"Anomalies: {summary.anomaly_count}")
    print(f"  Results: {config.output_dir}/{summary.run_id}/")
    print(f"  Chart: {config.output_dir}/{summary.run_id}/chart.html")


if __name__ == "__main__":
    main()
