"""Resolve loopback sandbox endpoints the same way scripts/demo.py does.

Order: environment variable, deploy/demo.local.env, deploy/demo.env, default.
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENV_FILES = [ROOT / "deploy" / "demo.env", ROOT / "deploy" / "demo.local.env"]


def _setting(key: str, default: str) -> str:
    """Read a setting from the environment, deploy/demo.local.env or deploy/demo.env, else the
    default.
    """
    if key in os.environ:
        return os.environ[key]
    entries: dict[str, str] = {}
    for file in ENV_FILES:
        if file.exists():
            entries.update(line.split("=", 1) for line in file.read_text().splitlines()
                           if line and not line.startswith("#") and "=" in line)
    return entries.get(key, default).strip()


def _port(key: str, default: int) -> int:
    """Read a port setting and check it is valid."""
    port = int(_setting(key, str(default)))
    if not 1 <= port <= 65535:
        raise ValueError(f"invalid port for {key}: {port}")
    return port


def app_url() -> str:
    """The loopback shop URL (frontend proxy)."""
    return f"http://127.0.0.1:{_port('FAULTLINE_APP_PORT', 8080)}"


def metrics_url() -> str:
    """The loopback Prometheus URL."""
    return f"http://127.0.0.1:{_port('FAULTLINE_METRICS_PORT', 9090)}"


def jaeger_api() -> str:
    """The Jaeger query API URL behind the shop's proxy."""
    # frontend-proxy routes /jaeger/ to Jaeger, whose query base path is /jaeger/ui.
    return app_url() + "/jaeger/ui/api"
