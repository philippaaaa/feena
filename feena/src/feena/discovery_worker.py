"""Subprocess entry point; the MCP supervisor controls its environment and deadline."""
import json
import os
import sys
from pathlib import Path

from .discovery import run_discovery
from .discovery_config import DiscoveryGoal


def main():
    os.umask(0o077)
    folder, target = Path(sys.argv[1]), sys.argv[2]
    try:
        goal = DiscoveryGoal.model_validate_json((folder / "discovery.json").read_text())
        result = run_discovery(goal, target, folder / "evidence")
        if result["status"] == "proposed":
            (folder / "draft-scenario.json").write_text(json.dumps(result["scenario"], indent=2))
        # This private result file is validated and filtered by the MCP supervisor.
    except Exception:  # noqa: BLE001 - sanitize worker errors
        result = {"status": "inconclusive", "reason": "Discovery could not complete."}
    (folder / "discovery-results.json").write_text(json.dumps(result))


if __name__ == "__main__":
    main()
