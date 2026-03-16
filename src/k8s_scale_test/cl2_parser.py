"""Parser for ClusterLoader2 JSON summary output."""

from __future__ import annotations

import json
from typing import Any

from k8s_scale_test.models import (
    APIAvailabilityResult,
    APILatencyResult,
    CL2ParseError,
    CL2Summary,
    CL2TestStatus,
    LatencyPercentile,
    SchedulingThroughputResult,
)


class CL2ResultParser:
    """Parses CL2 JSON summary output into structured data models."""

    @staticmethod
    def parse(raw_json: str) -> CL2Summary:
        """Parse CL2 JSON output into a CL2Summary.

        Raises CL2ParseError on invalid JSON or missing ``dataItems``.
        """
        try:
            data = json.loads(raw_json)
        except (json.JSONDecodeError, TypeError) as exc:
            raise CL2ParseError(f"Invalid JSON: {exc}") from exc

        if not isinstance(data, dict) or "dataItems" not in data:
            raise CL2ParseError("Missing required 'dataItems' field in CL2 JSON output")

        pod_latencies = CL2ResultParser.extract_pod_startup_latency(data)
        api_latencies = CL2ResultParser.extract_api_responsiveness(data)
        scheduling = CL2ResultParser.extract_scheduling_throughput(data)

        test_status = CL2TestStatus(
            config_name="unknown",
            job_name="unknown",
            status="Parsed",
            duration_seconds=0.0,
        )

        return CL2Summary(
            test_status=test_status,
            preload_plan=None,
            pod_startup_latencies=pod_latencies,
            api_latencies=api_latencies,
            api_availability=None,
            scheduling_throughput=scheduling,
            raw_results=data,
        )

    @staticmethod
    def extract_pod_startup_latency(data: dict) -> list[LatencyPercentile]:
        """Extract PodStartupLatency percentiles from CL2 data."""
        results: list[LatencyPercentile] = []
        for item in data.get("dataItems", []):
            labels = item.get("labels", {})
            if labels.get("Metric") != "pod_startup_latency":
                continue
            item_data = item.get("data", {})
            for key, percentile in (("Perc50", "P50"), ("Perc90", "P90"), ("Perc99", "P99")):
                if key in item_data:
                    results.append(
                        LatencyPercentile(
                            metric_name="PodStartupLatency",
                            percentile=percentile,
                            latency_ms=float(item_data[key]),
                        )
                    )
        return results

    @staticmethod
    def extract_api_responsiveness(data: dict) -> list[APILatencyResult]:
        """Extract APIResponsiveness metrics grouped by verb/resource."""
        results: list[APILatencyResult] = []
        for item in data.get("dataItems", []):
            labels = item.get("labels", {})
            if labels.get("Metric") != "api_responsiveness":
                continue
            item_data = item.get("data", {})
            results.append(
                APILatencyResult(
                    verb=labels.get("Verb", ""),
                    resource=labels.get("Resource", ""),
                    scope=labels.get("Scope", ""),
                    p50_ms=float(item_data.get("Perc50", 0.0)),
                    p90_ms=float(item_data.get("Perc90", 0.0)),
                    p99_ms=float(item_data.get("Perc99", 0.0)),
                    count=int(labels.get("Count", "0")),
                )
            )
        return results

    @staticmethod
    def extract_scheduling_throughput(data: dict) -> SchedulingThroughputResult | None:
        """Extract scheduling throughput (pods/sec). Returns None if not found."""
        for item in data.get("dataItems", []):
            labels = item.get("labels", {})
            if labels.get("Metric") != "scheduling_throughput":
                continue
            item_data = item.get("data", {})
            pods_per_second = float(item_data.get("Perc50", 0.0))
            return SchedulingThroughputResult(
                pods_per_second=pods_per_second,
                total_pods_scheduled=0,
                duration_seconds=0.0,
            )
        return None

    @staticmethod
    def extract_scheduling_throughput(data: dict) -> SchedulingThroughputResult | None:
        """Extract scheduling throughput (pods/sec). Returns None if not found."""
        for item in data.get("dataItems", []):
            labels = item.get("labels", {})
            if labels.get("Metric") != "scheduling_throughput":
                continue
            item_data = item.get("data", {})
            pods_per_second = float(item_data.get("Perc50", 0.0))
            return SchedulingThroughputResult(
                pods_per_second=pods_per_second,
                total_pods_scheduled=0,
                duration_seconds=0.0,
            )
        return None

    @staticmethod
    def extract_api_availability(data: dict) -> APIAvailabilityResult | None:
        """Extract API availability from CL2's APIAvailability JSON output.

        CL2 writes this as a separate file with structure:
        {"clusterMetrics": {"availabilityPercentage": 99.99, "longestUnavailablePeriod": "0s"}}
        """
        cluster = data.get("clusterMetrics")
        if not cluster:
            return None
        return APIAvailabilityResult(
            availability_percentage=float(cluster.get("availabilityPercentage", 0.0)),
            longest_unavailable_period=cluster.get("longestUnavailablePeriod", "unknown"),
        )

    @staticmethod
    def parse_report_dir(report_dir: str) -> CL2Summary:
        """Parse all CL2 output files from a report directory.

        Looks for:
        - PerfData JSON files (dataItems format) for latency/throughput metrics
        - APIAvailability JSON file for availability metrics
        """
        from pathlib import Path

        rd = Path(report_dir)
        perf_json = ""
        api_avail = None

        for jf in sorted(rd.glob("*.json")):
            try:
                content = jf.read_text()
                data = json.loads(content)

                # PerfData format (has dataItems)
                if "dataItems" in data and not perf_json:
                    perf_json = content

                # APIAvailability format (has clusterMetrics)
                if "clusterMetrics" in data:
                    api_avail = CL2ResultParser.extract_api_availability(data)

            except (json.JSONDecodeError, Exception):
                continue

        if not perf_json:
            raise CL2ParseError(f"No PerfData JSON found in {report_dir}")

        summary = CL2ResultParser.parse(perf_json)
        summary.api_availability = api_avail
        return summary
