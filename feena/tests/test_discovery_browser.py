"""Real browser discovery/replay and redirect boundary; no provider API calls."""
import importlib.util
import threading
from contextlib import contextmanager
from pathlib import Path

from flask import Flask, redirect
from werkzeug.serving import make_server

from feena.discovery import run_discovery
from feena.discovery_config import DiscoveryGoal
from feena.llm import Decision
from feena.simulation import run_simulations
from feena.simulation_config import BrowserScenario


@contextmanager
def serve(app):
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=3)


class ScriptedModel:
    available = True

    def __init__(self):
        self.calls = 0

    def decide(self, system, goal, snapshot, history):
        self.calls += 1
        action = "click" if self.calls == 1 else "done"
        return Decision(action, target="#checkout" if action == "click" else "",
                        raw={"action": action})


def test_discover_checkout_and_replay(tmp_path):
    source = Path(__file__).parents[1] / "examples/resilient-checkout/app.py"
    spec = importlib.util.spec_from_file_location("discovery_checkout", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    goal = DiscoveryGoal(name="discovered-checkout", goal="Place one order", assertions=[
        {"kind": "text", "target": "#status", "expected": "Order confirmed."},
        {"kind": "json", "target": "/api/state", "expected": {"orders": 1}},
    ])
    with serve(module.app) as target:
        result = run_discovery(goal, target, tmp_path / "discovery", ScriptedModel())
        assert result["status"] == "proposed", result
        scenario = BrowserScenario.model_validate(result["scenario"])
        replay = run_simulations([scenario], target, tmp_path / "replay")
        assert all(item.status == "passed" for item in replay), replay
    assert (tmp_path / "discovery/trace.zip").is_file()


def test_redirect_never_contacts_external_origin(tmp_path):
    external = Flask("discovery-external")
    received = []

    @external.route("/leak")
    def leak():
        received.append(True)
        return "external"

    with serve(external) as external_url:
        target_app = Flask("discovery-target")
        visited = []

        @target_app.route("/")
        def index():
            visited.append(True)
            return '<button id="checkout" onclick="location.href=\'/redirect\'">Go</button>'

        @target_app.route("/redirect")
        def redirect_out():
            return redirect(external_url + "/leak")

        with serve(target_app) as target:
            goal = DiscoveryGoal(name="redirect-check", goal="Click Go", assertions=[
                {"kind": "visible", "target": "#external-completion"}])
            result = run_discovery(goal, target, tmp_path, ScriptedModel())
            scenario = BrowserScenario(name="redirect-replay", goal=goal.goal, steps=[
                {"action": "goto", "target": "/"},
                {"action": "click", "target": "#checkout"},
            ], assertions=goal.assertions)
            replay = run_simulations([scenario], target, tmp_path / "replay")
            assert replay[0].status == "inconclusive"
    assert result["status"] == "inconclusive"
    assert visited, "The browser must reach the permitted target before testing redirect isolation"
    assert received == []
