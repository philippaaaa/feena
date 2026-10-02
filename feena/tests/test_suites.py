import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from click.testing import CliRunner
from pydantic import ValidationError

from feena import cli, simulation
from feena.config import Config, JourneySuite, TargetConfig
from feena.simulation import SimulationResult
from feena.simulation_config import BrowserScenario
from feena.suites import select_scenarios, summarize_run


def config(tmp_path):
    scenarios = [BrowserScenario(name=name, goal="Complete flow",
                 steps=[{"action": "goto", "target": "/"}],
                 assertions=[{"kind": "visible", "target": "button"}])
                 for name in ("login", "checkout", "settings")]
    return Config(target=TargetConfig(compose="", service="", port=1),
                  scenarios=scenarios,
                  suites=[JourneySuite(name="release", scenarios=["checkout", "login"])],
                  report={"out_dir": str(tmp_path)})


def test_suite_order_and_selection(tmp_path):
    cfg = config(tmp_path)
    assert [s.name for s in select_scenarios(cfg, "release")] == ["checkout", "login"]
    assert len(select_scenarios(cfg)) == 3
    with pytest.raises(ValueError):
        select_scenarios(cfg, "unknown")
    with pytest.raises(ValueError):
        select_scenarios(cfg, "release", "login")


@pytest.mark.parametrize("suites", [
    [{"name": "release", "scenarios": ["missing"]}],
    [{"name": "release", "scenarios": []}],
    [{"name": "release", "scenarios": ["login", "login"]}],
    [{"name": "release", "scenarios": ["login"]}] * 2,
])
def test_invalid_checklists_rejected(tmp_path, suites):
    data = config(tmp_path).model_dump()
    data["suites"] = suites
    with pytest.raises(ValidationError):
        Config(**data)


@pytest.mark.parametrize("statuses,expected", [
    (["passed", "passed"], "passed"),
    (["passed", "failed"], "failed"),
    (["passed", "inconclusive"], "inconclusive"),
    (["passed", "unknown"], "inconclusive"),
])
def test_aggregate_gate(tmp_path, statuses, expected):
    selected = select_scenarios(config(tmp_path), "release")
    results = [SimulationResult(s.name, "normal", status)
               for s, status in zip(selected, statuses)]
    assert summarize_run(selected, results)["status"] == expected


@pytest.mark.parametrize("kind", ["empty", "missing", "duplicate", "unexpected"])
def test_incomplete_execution_cannot_pass(tmp_path, kind):
    selected = select_scenarios(config(tmp_path), "release")
    results = [SimulationResult(s.name, "normal", "passed") for s in selected]
    if kind == "empty":
        results = []
    elif kind == "missing":
        results.pop()
    elif kind == "duplicate":
        results.append(results[0])
    else:
        results.append(SimulationResult("other", "normal", "passed"))
    summary = summarize_run(selected, results)
    assert summary["status"] == "inconclusive"
    assert summary["complete"] is False


def test_cli_suite_persists_history_without_agents(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda *args: cfg)

    @contextmanager
    def target(*args):
        yield SimpleNamespace(base_url="http://localhost")

    captured = []
    def run(scenarios, url, out):
        captured.append([s.name for s in scenarios])
        return [SimulationResult(s.name, "normal", "passed") for s in scenarios]

    monkeypatch.setattr(cli, "_target", target)
    monkeypatch.setattr(simulation, "run_simulations", run)
    runner = CliRunner()
    for _ in range(2):
        response = runner.invoke(cli.main, ["simulate", "--url", "http://localhost",
                                            "--suite", "release"])
        assert response.exit_code == 0, response.output
    assert captured == [["checkout", "login"]] * 2
    summaries = list((tmp_path / "suite-runs").glob("*/summary.json"))
    assert len(summaries) == 2
    assert json.loads(summaries[0].read_text())["suite"] == "release"
    response = runner.invoke(cli.main, ["suites"])
    assert response.exit_code == 0 and "checkout" in response.output
    response = runner.invoke(cli.main, ["simulate", "--url", "http://localhost",
                                        "--suite", "unknown"])
    assert response.exit_code != 0 and len(captured) == 2
