"""Flux2 GitOps repository reader and writer."""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from k8s_scale_test.models import DeploymentConfig, HelmReleaseConfig

log = logging.getLogger(__name__)

INFRASTRUCTURE_DIRS = {"infrastructure"}


class FluxRepoReader:
    """Parses Flux2 repo to discover Deployments and HelmReleases."""

    def __init__(self, repo_path: str) -> None:
        self.repo_path = Path(repo_path)

    def get_deployments(self) -> list[DeploymentConfig]:
        results: list[DeploymentConfig] = []
        for search_dir in [self.repo_path / "apps" / "base", self.repo_path / "apps" / "overlay-prod"]:
            if not search_dir.exists():
                continue
            for yaml_file in search_dir.rglob("*.yaml"):
                try:
                    docs = list(yaml.safe_load_all(yaml_file.read_text()))
                except Exception:
                    continue
                for doc in docs:
                    if not isinstance(doc, dict) or doc.get("kind") != "Deployment":
                        continue
                    meta = doc.get("metadata", {})
                    spec = doc.get("spec", {})
                    tmpl = spec.get("template", {}).get("spec", {})
                    reqs = {}
                    if tmpl.get("containers"):
                        reqs = tmpl["containers"][0].get("resources", {}).get("requests", {})
                    labels = meta.get("labels", {})
                    role = labels.get("scale-test/role")
                    ns = meta.get("namespace", "default")
                    kf = yaml_file.parent / "kustomization.yaml"
                    if kf.exists():
                        try:
                            k = yaml.safe_load(kf.read_text())
                            if isinstance(k, dict) and "namespace" in k:
                                ns = k["namespace"]
                        except Exception:
                            pass
                    results.append(DeploymentConfig(
                        name=meta.get("name", "unknown"), namespace=ns,
                        current_replicas=spec.get("replicas", 1),
                        resource_requests={str(k): str(v) for k, v in reqs.items()},
                        node_selector=tmpl.get("nodeSelector", {}),
                        tolerations=tmpl.get("tolerations", []),
                        affinity=tmpl.get("affinity", {}),
                        source_path=str(yaml_file.relative_to(self.repo_path)),
                        role=role,
                    ))
        return results

    def get_helm_releases(self) -> list[HelmReleaseConfig]:
        results: list[HelmReleaseConfig] = []
        for search_dir in [self.repo_path / "apps" / "base", self.repo_path / "apps" / "overlay-prod"]:
            if not search_dir.exists():
                continue
            for yaml_file in search_dir.rglob("*.yaml"):
                try:
                    docs = list(yaml.safe_load_all(yaml_file.read_text()))
                except Exception:
                    continue
                for doc in docs:
                    if not isinstance(doc, dict) or doc.get("kind") != "HelmRelease":
                        continue
                    meta = doc.get("metadata", {})
                    spec = doc.get("spec", {})
                    chart = spec.get("chart", {}).get("spec", {}).get("chart", "")
                    results.append(HelmReleaseConfig(
                        name=meta.get("name", "unknown"),
                        namespace=meta.get("namespace", "default"),
                        chart=chart,
                        source_path=str(yaml_file.relative_to(self.repo_path)),
                    ))
        return results


class FluxRepoWriter:
    """Updates deployment replica counts in the Flux2 GitOps repo.

    Raises PermissionError for infrastructure directory changes.
    """

    def __init__(self, repo_path: str) -> None:
        self.repo_path = Path(repo_path)

    def is_infrastructure_path(self, file_path: str) -> bool:
        return any(p in INFRASTRUCTURE_DIRS for p in Path(file_path).parts)

    def set_replicas(self, name: str, replicas: int, source_path: str) -> bool:
        if self.is_infrastructure_path(source_path):
            raise PermissionError(f"Cannot modify {source_path} without operator approval")
        full = self.repo_path / source_path
        if not full.exists():
            return False
        docs = list(yaml.safe_load_all(full.read_text()))
        modified = False
        for doc in docs:
            if not isinstance(doc, dict):
                continue
            if doc.get("kind") == "Deployment" and doc.get("metadata", {}).get("name") == name:
                doc["spec"]["replicas"] = replicas
                modified = True
        if modified:
            with open(full, "w") as fh:
                yaml.dump_all(docs, fh, default_flow_style=False, sort_keys=False)
            log.info("Set %s replicas=%d in %s", name, replicas, source_path)
        return modified

    def set_replicas_batch(self, targets: list[tuple[str, int, str]]) -> dict[str, bool]:
        results: dict[str, bool] = {}
        for name, replicas, path in targets:
            try:
                results[name] = self.set_replicas(name, replicas, path)
            except PermissionError as exc:
                log.error("%s", exc)
                results[name] = False
        return results

    def distribute_pods_across_deployments(
        self, deployments: list[DeploymentConfig], total_target: int,
    ) -> list[tuple[str, int, str]]:
        if not deployments:
            return []
        per = total_target // len(deployments)
        rem = total_target % len(deployments)
        return [
            (d.name, per + (1 if i < rem else 0), d.source_path)
            for i, d in enumerate(deployments)
        ]

    def set_resource_requests(
        self,
        name: str,
        cpu_request: str,
        memory_request: str,
        cpu_limit: str,
        memory_limit: str,
        source_path: str,
    ) -> bool:
        """Update resource requests and limits for a Deployment's first container."""
        if self.is_infrastructure_path(source_path):
            raise PermissionError(f"Cannot modify {source_path}")
        full = self.repo_path / source_path
        if not full.exists():
            return False
        docs = list(yaml.safe_load_all(full.read_text()))
        modified = False
        for doc in docs:
            if not isinstance(doc, dict):
                continue
            if doc.get("kind") == "Deployment" and doc.get("metadata", {}).get("name") == name:
                containers = doc["spec"]["template"]["spec"]["containers"]
                if containers:
                    containers[0].setdefault("resources", {})
                    containers[0]["resources"]["requests"] = {
                        "cpu": cpu_request,
                        "memory": memory_request,
                    }
                    containers[0]["resources"]["limits"] = {
                        "cpu": cpu_limit,
                        "memory": memory_limit,
                    }
                    modified = True
        if modified:
            with open(full, "w") as fh:
                yaml.dump_all(docs, fh, default_flow_style=False, sort_keys=False)
        return modified

    def distribute_pods_weighted(
        self,
        deployments: list[DeploymentConfig],
        total_target: int,
        weights: dict[str, float],
    ) -> list[tuple[str, int, str]]:
        """Distribute pods proportionally by weight.

        Each deployment gets floor(total * weight) pods.
        Remainder distributed round-robin from highest-weighted.
        """
        if not deployments:
            return []
        # Validate weights sum to ~1.0
        total_weight = sum(weights.get(d.name, 0.0) for d in deployments)
        if abs(total_weight - 1.0) > 0.01:
            raise ValueError(
                f"Weights sum to {total_weight:.4f}, must be 1.0 ± 0.01"
            )
        # Floor allocation
        result = []
        allocated = 0
        for d in deployments:
            w = weights.get(d.name, 0.0)
            count = int(total_target * w)  # floor
            result.append((d.name, count, d.source_path))
            allocated += count
        # Remainder round-robin from highest weight
        remainder = total_target - allocated
        sorted_indices = sorted(
            range(len(result)),
            key=lambda i: weights.get(result[i][0], 0.0),
            reverse=True,
        )
        for i in range(remainder):
            idx = sorted_indices[i % len(sorted_indices)]
            name, count, path = result[idx]
            result[idx] = (name, count + 1, path)
        return result

