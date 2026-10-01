import asyncio
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from feena.mcp_server import AccessToken, Runs, create_server, http_app


def manager(tmp_path):
    return Runs(Path("examples/resilient-checkout/feena.yaml"),
                "http://127.0.0.1:5056", tmp_path)


def test_http_auth_host_and_discovery(tmp_path):
    runs = manager(tmp_path)
    server = create_server(runs, ["testserver"])
    app = http_app(server, runs, "x" * 40)
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/").status_code == 200
        assert "Add to Cursor" in client.get("/").text
        assert "Connect Feena" in client.get("/").text
        assert "name=feena&config=" in client.get("/").text
        assert "Mallory" not in client.get("/").text
        assert "x" * 40 not in client.get("/").text
        assert client.get("/connection-info").json() == {"endpoint": None}
        assert client.get("/connection-check").status_code == 401
        assert client.get("/connection-check", headers={"Authorization": "Bearer wrong"}).status_code == 401
        response = client.get("/connection-check", headers={"Authorization": "Bearer " + "x" * 40})
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        assert client.get("/health").json()["service"] == "Feena MCP"
        assert client.post("/mcp", json={}).status_code == 401
        headers = {"Authorization": "Bearer " + "x" * 40,
                   "Accept": "application/json, text/event-stream"}
        payload = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                   "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                              "clientInfo": {"name": "test", "version": "1"}}}
        response = client.post("/mcp", json=payload, headers=headers)
        assert response.status_code == 200
        assert response.json()["result"]["serverInfo"]["name"] == "Feena UX QA"
        response = client.post("/mcp", headers=headers,
                               json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert {t["name"] for t in response.json()["result"]["tools"]} == {
            "list_scenarios", "start_run", "get_run", "cancel_run"}
        assert client.post("/mcp", json=payload,
                           headers={**headers, "Host": "evil.example"}).status_code == 421


def test_run_limits_cancel_and_unknown_inputs(tmp_path, monkeypatch):
    runs = manager(tmp_path)

    async def execute(run_id, folder):
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            runs.jobs[run_id]["status"] = "cancelled"

    monkeypatch.setattr(runs, "_execute", execute)

    async def check():
        with pytest.raises(ValueError, match="Unknown"):
            await runs.start("../../anything")
        result = await runs.start("retry-checkout")
        await asyncio.sleep(0)
        with pytest.raises(ValueError, match="already active"):
            await runs.start("retry-checkout")
        assert (await runs.cancel(result["run_id"]))["status"] == "cancelled"
        with pytest.raises(ValueError, match="Unknown"):
            runs.get("../../token")
        await runs.close()

    asyncio.run(check())


def test_job_survives_separate_stateless_requests(tmp_path, monkeypatch):
    import json

    runs = manager(tmp_path)

    async def execute(run_id, folder):
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            runs.jobs[run_id]["status"] = "cancelled"

    monkeypatch.setattr(runs, "_execute", execute)
    app = http_app(create_server(runs, ["testserver"]), runs, "x" * 40)
    headers = {"Authorization": "Bearer " + "x" * 40,
               "Accept": "application/json, text/event-stream"}
    with TestClient(app) as client:
        def call(name, arguments):
            result = client.post("/mcp", headers=headers, json={
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": name, "arguments": arguments}}).json()["result"]
            assert not result.get("isError")
            return json.loads(result["content"][0]["text"])

        run = call("start_run", {"scenario": "retry-checkout"})
        assert call("get_run", {"run_id": run["run_id"]})["status"] == "running"
        assert call("cancel_run", {"run_id": run["run_id"]})["status"] == "cancelled"


@pytest.mark.parametrize("url", ["http://example.com/mcp", "https://a:b@example.com/mcp",
                                 "https://example.com/mcp?token=secret", "https://example.com/"])
def test_invalid_public_endpoint_rejected(url):
    with pytest.raises(ValueError):
        AccessToken(None, "x" * 40, url)


def test_public_endpoint_is_operator_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("FEENA_MCP_PUBLIC_URL", "https://qa.example.com/mcp")
    runs = manager(tmp_path)
    with TestClient(http_app(create_server(runs, ["testserver"]), runs, "x" * 40)) as client:
        assert client.get("/connection-info").json() == {"endpoint": "https://qa.example.com/mcp"}


def test_campaign_mcp_and_legacy_run_share_durable_queue(tmp_path, monkeypatch):
    import json

    from feena.campaigns import Campaigns
    from feena.mcp_server import CampaignRuns

    campaigns = Campaigns(Path("examples/resilient-checkout/feena.yaml"),
                          ["http://127.0.0.1:5056"], tmp_path)
    # No browser is needed to verify routing, persistence, and cancellation through MCP.
    async def resume():
        pass
    monkeypatch.setattr(campaigns, "resume", resume)
    runs = CampaignRuns(campaigns)
    app = http_app(create_server(runs, ["testserver"], campaigns), runs, "x" * 40)
    headers = {"Authorization": "Bearer " + "x" * 40,
               "Accept": "application/json, text/event-stream"}
    with TestClient(app) as client:
        def call(name, arguments):
            response = client.post("/mcp", headers=headers, json={
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": name, "arguments": arguments}})
            result = response.json()["result"]
            assert not result.get("isError"), result
            return json.loads(result["content"][0]["text"])

        campaign = call("start_campaign", {"scenarios": ["retry-checkout"]})
        campaign_id = campaign["campaign_id"]
        assert call("get_campaign", {"campaign_id": campaign_id})["campaign_id"] == campaign_id
        legacy = call("start_run", {"scenario": "offline-recovery"})
        assert legacy["run_id"] == legacy["campaign_id"]
        assert call("get_run", {"run_id": legacy["run_id"]})["campaign_id"] == legacy["run_id"]
        assert call("cancel_campaign", {"campaign_id": campaign_id})["status"] == "cancelled"
        assert call("cancel_run", {"run_id": legacy["run_id"]})["status"] == "cancelled"


@pytest.mark.parametrize("outcome,expected", [
    ("passed", "completed"), ("failed", "completed"),
    ("inconclusive", "completed"), ("timed_out", "timed_out"), ("error", "error"),
])
def test_legacy_campaign_adapter_preserves_execution_status(outcome, expected):
    from feena.mcp_server import CampaignRuns

    campaign_status = "inconclusive" if outcome in {"inconclusive", "timed_out", "error"} else (
        "failed" if outcome == "failed" else "completed")
    result = CampaignRuns._run({"campaign_id": "sample", "status": campaign_status, "jobs": [
        {"scenario": "checkout", "profile": "normal", "status": outcome, "reason": None}]})
    assert result["status"] == expected
    assert result["scenario"] == "checkout"
    assert result["results"][0]["status"] == outcome
