"""Load the typed capability catalog and execution policy (P0-04)."""
from __future__ import annotations

import difflib
import os
from pathlib import Path

import yaml

from .schemas import Capability, Execution

ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT / "config" / "capabilities.yaml"
POLICY_PATH = ROOT / "config" / "execution_policy.yaml"


class Unsupported(LookupError):
    """Structured refusal the API/agent can show instead of guessing."""

    def __init__(self, code: str, message: str, **details):
        """Create the error with a stable code, a message and details for the caller."""
        super().__init__(message)
        self.code, self.message, self.details = code, message, details

    def as_dict(self) -> dict:
        """The error as a JSON-safe dict."""
        return {"error": self.code, "message": self.message, **self.details}


class Catalog:
    """The typed fault capability catalog loaded from config/capabilities.yaml."""
    def __init__(self, path: Path = CATALOG_PATH):
        """Load and validate every capability, rejecting duplicate ids."""
        raw = yaml.safe_load(path.read_text())
        self.version = raw["catalog_version"]
        items = [Capability.model_validate(c) for c in raw["capabilities"]]
        self.by_id = {c.id: c for c in items}
        if len(self.by_id) != len(items):
            raise ValueError("duplicate capability IDs in catalog")

    def get(self, capability_id: str) -> Capability:
        """Return a capability by id, or raise Unsupported with the closest matches."""
        if capability_id not in self.by_id:
            raise Unsupported(
                "UNSUPPORTED_CAPABILITY", f"No capability named {capability_id!r}.",
                closest=difflib.get_close_matches(capability_id, list(self.by_id), n=3),
                supported=sorted(self.by_id))
        return self.by_id[capability_id]

    def plannable(self, capability_id: str) -> Capability:
        """Return a capability that may be planned, raising Unsupported if it is disabled."""
        cap = self.get(capability_id)
        if cap.execution == Execution.DISABLED:
            raise Unsupported("CAPABILITY_DISABLED", f"{cap.id} is not available: {cap.disabled_reason}",
                              prerequisites=cap.prerequisites)
        return cap

    def summaries(self) -> list[dict]:
        """Short summaries of all capabilities (id, title, family, status, parameters, max
        duration).
        """
        return [{"id": c.id, "title": c.title, "family": c.family.value,
                 "execution": c.execution.value, "disabled_reason": c.disabled_reason,
                 "parameters": [p.model_dump() for p in c.parameters],
                 "max_duration_seconds": c.duration.max_seconds}
                for c in self.by_id.values()]


def load_policy(path: Path = POLICY_PATH) -> dict:
    """Load the execution policy (FAULTLINE_EXECUTION_POLICY overrides the default file)."""
    if path == POLICY_PATH and os.environ.get("FAULTLINE_EXECUTION_POLICY"):
        path = Path(os.environ["FAULTLINE_EXECUTION_POLICY"])
    return yaml.safe_load(path.read_text())


def is_live(policy: dict, capability_id: str) -> bool:
    """True if live execution is enabled and this capability is listed as live."""
    return bool(policy.get("execution_enabled")) and capability_id in policy.get("live_capabilities", [])
