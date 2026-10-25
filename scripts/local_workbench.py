"""Start the connected workbench using reviewed, host-local configuration."""
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]

if __name__ == "__main__":
    for variable, filename in (("FAULTLINE_SANDBOX_SPEC", "sandbox.local.yaml"),
                               ("FAULTLINE_EXECUTION_POLICY", "execution_policy.local.yaml")):
        path = ROOT / "config" / filename
        if not path.is_file():
            raise SystemExit(f"Missing {path}; complete local scope/baseline setup first.")
        os.environ[variable] = str(path)
    os.chdir(ROOT)
    os.execv(sys.executable, [sys.executable, "-m", "workbench.controller_server", "--port", "8095"])
