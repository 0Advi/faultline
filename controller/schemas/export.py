"""Write JSON Schema files for the UI and agent: python -m controller.schemas.export"""
import json
from pathlib import Path
import sys

from . import contracts

OUT = Path(__file__).resolve().parents[2] / "schemas"
MODELS = ["Capability", "Plan", "Run", "Event", "Observation", "Diagnosis", "Patch", "Comparison",
          "Baseline", "PaymentPolicy", "RunReport", "DiagnosisResult"]


def main(check: bool = False) -> int:
    """Write the JSON Schema of every contract to schemas/, or with check=True report schemas that
    are stale.
    """
    OUT.mkdir(exist_ok=True)
    stale = []
    for name in MODELS:
        schema = getattr(contracts, name).model_json_schema()
        schema["$id"] = f"faultline/{name.lower()}/{contracts.SCHEMA_VERSION}"
        text = json.dumps(schema, indent=2, sort_keys=True) + "\n"
        path = OUT / f"{name.lower()}.schema.json"
        if check:
            if not path.exists() or path.read_text() != text:
                stale.append(path.name)
        else:
            path.write_text(text)
    if stale:
        print("Stale schemas (run python -m controller.schemas.export):", ", ".join(stale))
        return 1
    print(f"{'Checked' if check else 'Wrote'} {len(MODELS)} schemas in {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main(check="--check" in sys.argv))
