"""Select release checklists and persist complete, fail-closed run summaries."""
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path


def select_scenarios(config, suite_name=None, scenario_name=None):
    if suite_name and scenario_name:
        raise ValueError("Choose --suite or --scenario, not both.")
    by_name = {s.name: s for s in config.scenarios}
    if suite_name:
        suite = next((s for s in config.suites if s.name == suite_name), None)
        if suite is None:
            raise ValueError(f"Unknown suite: {suite_name}. Use feena suites to list checklists.")
        return [by_name[name] for name in suite.scenarios]
    selected = [s for s in config.scenarios if scenario_name is None or s.name == scenario_name]
    if not selected:
        raise ValueError("No matching scenarios configured; add scenarios to feena.yaml.")
    return selected


def summarize_run(scenarios, results, suite_name=None):
    expected = [(s.name, p.name) for s in scenarios for p in s.profiles]
    observed = Counter((r.name, r.profile) for r in results)
    expected_set = set(expected)
    complete = bool(expected) and observed == Counter(expected)
    rows = []
    for name, profile in expected:
        matches = [r for r in results if (r.name, r.profile) == (name, profile)]
        if len(matches) != 1:
            rows.append({"name": name, "profile": profile, "status": "inconclusive",
                             "reason": "Missing or duplicate profile result", "artifacts": None})
            continue
        r = matches[0]
        status = r.status if r.status in ("passed", "failed", "inconclusive") else "inconclusive"
        rows.append({"name": name, "profile": profile, "status": status,
                         "reason": r.reason, "artifacts": str(r.artifacts) if r.artifacts else None})
    unexpected = [{"name": r.name, "profile": r.profile} for r in results
                  if (r.name, r.profile) not in expected_set]
    counts = dict(Counter(row["status"] for row in rows))
    status = ("failed" if counts.get("failed") else
              "inconclusive" if not complete or counts.get("inconclusive") else "passed")
    return {"version": 1, "created_at": datetime.now(UTC).isoformat(), "suite": suite_name,
                "status": status, "complete": complete, "counts": counts, "results": rows,
                "unexpected_results": unexpected}


def write_summary(summary, directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
