"""Sandbox identity and scope checks (P0-01).

collect_snapshot() reads the Docker daemon; evaluate() is a pure function so
positive and negative fixtures can be tested without Docker.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
import sys
from pathlib import Path
import subprocess

import yaml

ROOT = Path(__file__).resolve().parents[2]
SPEC_PATH = ROOT / "config" / "sandbox.yaml"
PROJECT_LABEL = "com.docker.compose.project"
TOOL_LABEL = "faultline.tool"  # short-lived curl/grpcurl containers started by the controller
SERVICE_LABEL = "com.docker.compose.service"


def load_spec(path: Path = SPEC_PATH) -> dict:
    """Load the sandbox spec YAML (path can be overridden with FAULTLINE_SANDBOX_SPEC)."""
    if path == SPEC_PATH and os.environ.get("FAULTLINE_SANDBOX_SPEC"):
        path = Path(os.environ["FAULTLINE_SANDBOX_SPEC"])
    return yaml.safe_load(path.read_text())


def _docker(*args: str) -> str:
    """Run a docker command and return its stdout."""
    return subprocess.run(["docker", *args], check=True, capture_output=True,
                          text=True, timeout=30).stdout


def collect_snapshot() -> dict:
    """Read daemon identity and every container on it (not only the project)."""
    context = _docker("context", "show").strip()
    info = json.loads(_docker("info", "--format", "{{json .}}"))
    ids = _docker("ps", "--all", "--quiet", "--no-trunc").split()
    containers = []
    for item in (json.loads(_docker("inspect", *ids)) if ids else []):
        labels = item["Config"].get("Labels") or {}
        containers.append({
            "id": item["Id"],
            "name": item["Name"].lstrip("/"),
            "project": labels.get(PROJECT_LABEL),
            "service": labels.get(SERVICE_LABEL),
            "tool": labels.get(TOOL_LABEL),
            "state": item["State"]["Status"],
            "privileged": item["HostConfig"].get("Privileged", False),
            "network_mode": item["HostConfig"].get("NetworkMode", ""),
            "networks": sorted((item["NetworkSettings"].get("Networks") or {}).keys()),
            "mounts": [{"type": m["Type"], "source": m.get("Source", ""),
                        "destination": m["Destination"], "rw": m.get("RW", True)}
                       for m in item.get("Mounts", [])],
        })
    return {"docker_context": context, "daemon_id": info.get("ID", ""),
            "daemon_name": info.get("Name", ""), "containers": containers}


@dataclass
class ScopeReport:
    """Result of checking the live Docker environment against the sandbox spec: errors, warnings and
    resolved targets.
    """
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    targets: dict[str, str] = field(default_factory=dict)
    approved: bool = False

    @property
    def scope_ok(self) -> bool:
        """True when no scope errors were found."""
        return not self.errors

    @property
    def isolation_approved(self) -> bool:
        """Only this permits live execution."""
        return self.scope_ok and self.approved

    def as_dict(self) -> dict:
        """The report as a JSON-friendly dict."""
        return {"scope_ok": self.scope_ok, "isolation_approved": self.isolation_approved,
                "errors": self.errors, "warnings": self.warnings, "targets": self.targets}


def _under(path: str, roots: list[Path]) -> bool:
    """Whether a (bind-mount) path lies under one of the allowed roots, mapping Docker Desktop's
    /host_mnt prefix on macOS.
    """
    candidate = Path(path)
    # Docker Desktop exposes macOS bind paths through its Linux VM prefix.
    if sys.platform == "darwin" and path.startswith("/host_mnt/"):
        host_path = Path(path.removeprefix("/host_mnt"))
        if host_path.exists():
            candidate = host_path
    resolved = candidate.resolve(strict=False)
    return any(resolved == root or root in resolved.parents for root in roots)


def evaluate(snapshot: dict, spec: dict, repo_root: Path = ROOT) -> ScopeReport:
    """Compare a Docker snapshot with the sandbox spec (context, daemon, project, containers,
    networks, published ports, mounts) and report errors and warnings.
    """
    report = ScopeReport()
    err, warn = report.errors.append, report.warnings.append

    if spec.get("approved") is not True:
        warn("sandbox spec is not approved: this is a shared/preview environment")
    if snapshot["docker_context"] != spec["docker_context"]:
        err(f"docker context {snapshot['docker_context']!r} != {spec['docker_context']!r}")
    if not spec.get("daemon_id"):
        err("spec daemon_id is empty: record the dedicated daemon identity first")
    elif snapshot["daemon_id"] != spec["daemon_id"]:
        err("docker daemon ID differs from the approved sandbox daemon")

    project = spec["compose_project"]
    ours = [c for c in snapshot["containers"] if c.get("project") == project]
    foreign = [c["name"] for c in snapshot["containers"]
               if c.get("project") != project and not c.get("tool")]
    if foreign and spec.get("require_dedicated_daemon", True):
        err(f"daemon is shared with unrelated containers: {sorted(foreign)[:10]}")

    by_service: dict[str, list[dict]] = {}
    for c in ours:
        by_service.setdefault(c.get("service") or "", []).append(c)
    expected = set(spec["expected_services"])
    if unknown := sorted(set(by_service) - expected):
        err(f"unexpected services in project: {unknown}")
    if missing := sorted(expected - set(by_service)):
        err(f"expected services missing: {missing}")

    roots = [(repo_root / r).resolve() for r in spec.get("allowed_bind_roots", [])]
    exceptions = spec.get("bind_exceptions") or {}
    for service, items in sorted(by_service.items()):
        if len(items) != 1:
            err(f"{service}: expected exactly one container, found {len(items)}")
        for c in items:
            if c.get("privileged"):
                err(f"{service}: privileged container")
            if c.get("network_mode") == "host":
                err(f"{service}: host network mode")
            if spec["network"] not in c.get("networks", []):
                err(f"{service}: not attached to network {spec['network']}")
            for m in c.get("mounts", []):
                if m["type"] != "bind" or _under(m["source"], roots):
                    continue
                if m["source"] in exceptions.get(service, []) and not m.get("rw", True):
                    warn(f"{service}: accepted upstream bind {m['source']} -> "
                         f"{m['destination']} ({'rw' if m['rw'] else 'ro'})")
                else:
                    err(f"{service}: bind mount outside allowed roots or writable host exception: {m['source']}")

    for service in spec.get("mutable_services", []):
        items = by_service.get(service, [])
        if len(items) == 1:
            report.targets[service] = items[0]["id"]
    report.approved = spec.get("approved") is True
    return report


def resolve_target(report: ScopeReport, service: str) -> str:
    """Return the single container ID an adapter may touch, or raise."""
    if not report.isolation_approved:
        raise PermissionError("sandbox isolation is not approved; refusing to resolve targets")
    if service not in report.targets:
        raise PermissionError(f"{service} is not a mutable service in this sandbox")
    return report.targets[service]


def main() -> int:
    """Print the scope report for the current Docker environment; exit 0 only if isolation is
    approved.
    """
    report = evaluate(collect_snapshot(), load_spec())
    print(json.dumps(report.as_dict(), indent=2))
    return 0 if report.isolation_approved else 2


if __name__ == "__main__":
    raise SystemExit(main())
