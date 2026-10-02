import asyncio
import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from feena.campaigns import Campaigns
from feena.config import load_config
from feena.mcp_server import CampaignRuns, create_server, http_app

CONFIG = Path("examples/resilient-checkout/feena.yaml")


def test_discovery_tools_share_persistent_http_lifecycle(tmp_path, monkeypatch):
    campaigns = Campaigns(CONFIG, ["http://127.0.0.1:5056"], tmp_path)

    async def execute(job, target):
        await asyncio.Event().wait()

    monkeypatch.setattr(campaigns, "_execute", execute)
    runs = CampaignRuns(campaigns)
    app = http_app(create_server(runs, ["testserver"], campaigns), runs, "x" * 40)
    headers = {"Authorization": "Bearer " + "x" * 40,
               "Accept": "application/json, text/event-stream"}
    with TestClient(app) as client:
        def call(name, arguments):
            result = client.post("/mcp", headers=headers, json={
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": name, "arguments": arguments}}).json()["result"]
            assert not result.get("isError"), result
            if name == "list_discovery_goals":
                return result["structuredContent"]["result"]
            return json.loads(result["content"][0]["text"])

        goals = call("list_discovery_goals", {})
        assert goals[0]["name"] == "discovered-checkout"
        started = call("start_discovery", {"goal": "discovered-checkout"})
        key = started["campaign_id"]
        assert call("get_discovery", {"campaign_id": key})["status"] in {"running", "queued"}
        assert call("cancel_campaign", {"campaign_id": key})["status"] == "cancelled"


def test_config_rejects_duplicate_discovery_goals():
    cfg = load_config(CONFIG)
    payload = cfg.model_dump()
    payload["discovery"] = payload["discovery"] * 2
    with pytest.raises(ValueError, match="discovery goal names"):
        type(cfg).model_validate(payload)
