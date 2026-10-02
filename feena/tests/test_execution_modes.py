from contextlib import contextmanager
from types import SimpleNamespace

import click
import pytest
from click.testing import CliRunner
from pydantic import ValidationError

from feena import cli
from feena.config import Config, RunConfig, TargetConfig


def config():
    return Config(target=TargetConfig(compose="", service="", port=1))


@pytest.mark.parametrize("saved,mode,agent,expected", [
    ({}, None, None, ["regular", "clumsy", "hostile"]),
    ({}, "single", None, ["regular"]),
    ({"mode": "single", "single_agent": "clumsy"}, None, None, ["clumsy"]),
    ({"mode": "single"}, "swarm", None, ["regular", "clumsy", "hostile"]),
    ({}, None, "hostile", ["hostile"]),
    ({"mode": "single", "single_agent": "hostile"}, None, "clumsy", ["clumsy"]),
    ({"agents": ["clumsy", "regular", "clumsy"]}, "swarm", None, ["clumsy", "regular"]),
])
def test_selection_precedence(saved, mode, agent, expected):
    cfg = config()
    cfg.run = RunConfig(**saved)
    assert cli._select_agents(cfg, mode, agent) == expected


@pytest.mark.parametrize("values", [{"mode": "typo"}, {"single_agent": "unknown"}])
def test_invalid_config_rejected(values):
    with pytest.raises(ValidationError):
        RunConfig(**values)


@pytest.mark.parametrize("command", ["run", "ci"])
@pytest.mark.parametrize("args,expected", [
    (["--mode", "single"], ["regular"]),
    (["--agent", "clumsy"], ["clumsy"]),
    (["--mode", "swarm"], ["regular", "clumsy", "hostile"]),
])
def test_cli_dispatches_selected_agents(monkeypatch, command, args, expected):
    cfg = config()
    seen = []

    @contextmanager
    def target(*args):
        yield SimpleNamespace(base_url="http://localhost")

    def scan(cfg, sandbox, agents, blast):
        seen.extend(agents)
        raise click.ClickException("selection captured")

    monkeypatch.setattr(cli, "_load_or_default", lambda *args: cfg)
    monkeypatch.setattr(cli, "_target", target)
    monkeypatch.setattr(cli, "_scan", scan)
    monkeypatch.setattr(cli, "make_decider", lambda *args: SimpleNamespace(available=True))
    result = CliRunner().invoke(cli.main, [command, "--url", "http://localhost", *args])
    assert "selection captured" in result.output
    assert seen == expected


@pytest.mark.parametrize("command", ["run", "ci"])
def test_conflicting_flags_fail_before_target(monkeypatch, command):
    monkeypatch.setattr(cli, "_load_or_default", lambda *args: config())
    result = CliRunner().invoke(cli.main, [command, "--url", "http://localhost",
                                          "--mode", "swarm", "--agent", "regular"])
    assert result.exit_code == 2
    assert "--agent selects one agent" in result.output


def test_single_missing_model_fails_instead_of_empty_success(monkeypatch):
    cfg = config()
    cfg.run.mode = "single"
    monkeypatch.setattr(cli, "make_decider", lambda *args: SimpleNamespace(available=False))
    with pytest.raises(click.ClickException, match="requires configured model credentials"):
        cli._scan(cfg, SimpleNamespace(), ["regular"])


def test_agent_runner_single_does_not_launch_other_personas(monkeypatch, tmp_path):
    cfg = config()
    cfg.report.out_dir = str(tmp_path)
    cfg.run.mode = "single"
    seen = []

    @contextmanager
    def session(*args, **kwargs):
        yield SimpleNamespace()

    class Agent:
        def __init__(self, ctx):
            self.ctx = ctx

        def run(self):
            seen.append("regular")
            return []

    class Unexpected:
        def __init__(self, *args):
            pytest.fail("single mode launched another agent")

    monkeypatch.setattr(cli, "session", session)
    monkeypatch.setattr(cli, "EXPLORATORY", {"regular": Agent, "clumsy": Unexpected})
    monkeypatch.setattr(cli, "HostileAgent", Unexpected)
    agents = cli._select_agents(cfg, None, None)
    cli._run_agents(cfg, SimpleNamespace(base_url="http://localhost"), agents, SimpleNamespace())
    assert seen == ["regular"]
