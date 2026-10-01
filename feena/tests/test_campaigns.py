import asyncio
import json
from pathlib import Path

import pytest

from feena.campaigns import Campaigns

CONFIG = Path("examples/resilient-checkout/feena.yaml")


def manager(tmp_path, targets=None):
    return Campaigns(CONFIG, targets or ["http://127.0.0.1:5056"], tmp_path)


def test_validation_and_single_owner(tmp_path):
    async def check():
        runs = manager(tmp_path)
        with pytest.raises(ValueError, match="owned"):
            manager(tmp_path)
        with pytest.raises(ValueError, match="Unknown"):
            await runs.start(["../../anything"])
        with pytest.raises(ValueError, match="distinct"):
            await runs.start(["retry-checkout", "retry-checkout"])
        with pytest.raises(ValueError, match="Unknown"):
            runs.get("../anything")
        await runs.close()
    asyncio.run(check())


def test_bounded_workers_snapshot_and_cancellation(tmp_path, monkeypatch):
    async def check():
        runs = manager(tmp_path, ["http://127.0.0.1:5056", "http://127.0.0.1:5057"])
        active = set()
        peak = 0
        cleaned = []
        async def execute(job, target):
            nonlocal peak
            assert target not in active
            active.add(target)
            peak = max(peak, len(active))
            assert len(json.loads(job["snapshot"])["profiles"]) == 1
            try:
                await asyncio.Event().wait()
            finally:
                active.remove(target)
                cleaned.append(job["id"])
        monkeypatch.setattr(runs, "_execute", execute)
        first = await runs.start(["retry-checkout"])
        second = await runs.start(["retry-checkout"])
        await asyncio.sleep(0)
        assert peak == 2
        result = await runs.cancel(first["campaign_id"])
        assert result["status"] == "cancelled"
        assert all(job["status"] == "cancelled" for job in result["jobs"])
        assert cleaned
        await asyncio.sleep(0)
        assert peak == 2
        await runs.cancel(second["campaign_id"])
        assert not active
        await runs.close()
    asyncio.run(check())


def test_restart_preserves_snapshots_and_does_not_replay_running(tmp_path, monkeypatch):
    async def check():
        runs = manager(tmp_path)
        async def execute(job, target):
            await asyncio.Event().wait()
        monkeypatch.setattr(runs, "_execute", execute)
        first = await runs.start(["retry-checkout"])
        second = await runs.start(["retry-checkout"])
        await asyncio.sleep(0)
        await runs.close()
        # Simulate abrupt shutdown state as well as graceful interruption.
        import sqlite3
        with sqlite3.connect(tmp_path / "campaigns.sqlite3") as db:
            db.execute("UPDATE jobs SET status='running' WHERE id=?", (first["jobs"][0]["id"],))
        with pytest.raises(ValueError, match="operator recovery"):
            manager(tmp_path)
        resumed = Campaigns(CONFIG, ["http://127.0.0.1:5056"], tmp_path, recover_interrupted=True)
        assert resumed.get(first["campaign_id"])["jobs"][0]["status"] == "inconclusive"
        executed = []
        async def finish(job, target):
            executed.append(job["id"])
            return "passed", None
        monkeypatch.setattr(resumed, "_execute", finish)
        await resumed.resume()
        await asyncio.gather(*resumed.tasks.values())
        assert first["jobs"][0]["id"] not in executed
        assert resumed.get(second["campaign_id"])["status"] == "completed"
        assert (await resumed.cancel(second["campaign_id"]))["status"] == "completed"
        await resumed.close()
    asyncio.run(check())


def test_reset_failure_skips_worker(tmp_path, monkeypatch):
    async def check():
        runs = Campaigns(CONFIG, ["http://127.0.0.1:5056"], tmp_path, reset_path="/test/reset")
        class Client:
            def __init__(self, **kwargs):
                assert kwargs["trust_env"] is False
                assert kwargs["follow_redirects"] is False
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            async def post(self, url):
                assert url == "http://127.0.0.1:5056/test/reset"
                return type("Response", (), {"status_code": 302})()
        monkeypatch.setattr("feena.campaigns.httpx.AsyncClient", Client)
        result = await runs.start(["retry-checkout"])
        await asyncio.gather(*runs.tasks.values())
        assert runs.get(result["campaign_id"])["status"] == "inconclusive"
        assert not list(tmp_path.glob("*/scenario.json"))
        await runs.close()
    asyncio.run(check())


def test_store_configuration_cannot_change(tmp_path):
    async def check():
        runs = manager(tmp_path)
        await runs.close()
        with pytest.raises(ValueError, match="differ"):
            manager(tmp_path, ["http://127.0.0.1:6000"])
        with pytest.raises(ValueError, match="differ"):
            Campaigns(CONFIG, ["http://127.0.0.1:5056"], tmp_path, reset_path="/reset")
        runs = manager(tmp_path)
        await runs.close()
    asyncio.run(check())


def test_cancel_kills_process_group_before_releasing_slot(tmp_path, monkeypatch):
    async def check():
        runs = manager(tmp_path)
        launched = asyncio.Event()
        killed = asyncio.Event()
        calls = []
        class Process:
            pid = 543210
            returncode = None
            async def wait(self):
                await killed.wait()
                self.returncode = -9
                calls.append("reaped")
                return -9
        async def spawn(*args, **kwargs):
            assert kwargs["start_new_session"] is True
            assert "FEENA_MCP_TOKEN" not in kwargs["env"]
            launched.set()
            return Process()
        def killpg(pid, sig):
            assert pid == Process.pid
            calls.append("killed")
            killed.set()
        monkeypatch.setenv("FEENA_MCP_TOKEN", "must-not-reach-worker")
        monkeypatch.setattr("feena.campaigns.asyncio.create_subprocess_exec", spawn)
        monkeypatch.setattr("feena.campaigns.os.killpg", killpg)
        campaign = await runs.start(["retry-checkout"])
        await launched.wait()
        result = await runs.cancel(campaign["campaign_id"])
        assert result["status"] == "cancelled"
        assert calls == ["killed", "reaped"]
        await runs.close()
    asyncio.run(check())


@pytest.mark.parametrize("cancel", [True, False])
def test_real_process_group_cleanup(tmp_path, monkeypatch, cancel):
    import os
    import sys

    async def check():
        runs = Campaigns(CONFIG, ["http://127.0.0.1:5056"], tmp_path, timeout=1)
        original_spawn = asyncio.create_subprocess_exec
        launched = asyncio.Event()
        processes = []
        async def sleeper(*args, **kwargs):
            process = await original_spawn(sys.executable, "-c", "import time; time.sleep(60)", **kwargs)
            processes.append(process)
            launched.set()
            return process
        monkeypatch.setattr("feena.campaigns.asyncio.create_subprocess_exec", sleeper)
        campaign = await runs.start(["retry-checkout"])
        await launched.wait()
        if cancel:
            await asyncio.gather(runs.cancel(campaign["campaign_id"]), runs.cancel(campaign["campaign_id"]))
            assert runs.get(campaign["campaign_id"])["status"] == "cancelled"
        else:
            await asyncio.gather(*runs.tasks.values())
            assert runs.get(campaign["campaign_id"])["status"] == "inconclusive"
        for process in processes:
            assert process.returncode is not None
            with pytest.raises(ProcessLookupError):
                os.killpg(process.pid, 0)
        await runs.close()
    asyncio.run(check())
