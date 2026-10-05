"""Durable single-service campaigns over operator-provisioned disposable target slots.

One process owns a store. Each target gets one worker; different URLs must refer to
independent backend data. Targets require operator reset between campaigns when the
journey is not self-resetting. Interrupted jobs are never automatically retried.
"""
from __future__ import annotations

import asyncio
import fcntl
import json
import os
import signal
import sqlite3
import sys
import uuid
from pathlib import Path

import httpx

from .config import load_config
from .sandbox import attach
from .simulation_config import BrowserScenario, NetworkProfile, local_path


class Campaigns:
    MAX_JOBS = 1000
    MAX_CAMPAIGN_JOBS = 100

    def __init__(self, config: Path, targets: list[str], out: Path, timeout: int = 120, reset_path: str | None = None,
                 recover_interrupted: bool = False):
        if not 1 <= len(targets) <= 16 or not 1 <= timeout <= 3600:
            raise ValueError("Configure 1–16 targets and a timeout between 1 and 3600 seconds")
        self.reset_path = local_path(reset_path) if reset_path is not None else None
        self.targets = [attach(t).base_url for t in targets]
        if len(set(self.targets)) != len(self.targets):
            raise ValueError("Targets must be distinct disposable backend slots")
        configuration = load_config(config)
        self.scenarios = configuration.scenarios
        self.discovery_goals = configuration.discovery
        if not self.scenarios and not self.discovery_goals:
            raise ValueError("Configure at least one scenario or discovery goal")
        self.out = out.resolve()
        self.out.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lock = (self.out / "campaigns.lock").open("a")
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._lock.close()
            raise ValueError("Campaign store is already owned by another service") from None
        self.db = sqlite3.connect(self.out / "campaigns.sqlite3", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS recovery (required INTEGER NOT NULL);
            INSERT INTO recovery SELECT 0 WHERE NOT EXISTS (SELECT 1 FROM recovery);
            CREATE TABLE IF NOT EXISTS campaigns (id TEXT PRIMARY KEY, cancelled INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS jobs (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE, campaign TEXT,
                scenario TEXT, profile TEXT, snapshot TEXT, status TEXT, reason TEXT);
        """)
        columns = {r[1] for r in self.db.execute("PRAGMA table_info(jobs)")}
        for name, declaration in (("kind", "TEXT NOT NULL DEFAULT 'simulation'"), ("draft", "TEXT")):
            if name not in columns:
                self.db.execute(f"ALTER TABLE jobs ADD COLUMN {name} {declaration}")
        self.db.execute("CREATE TABLE IF NOT EXISTS approved_discoveries (source TEXT PRIMARY KEY, scenario TEXT NOT NULL, validation_campaign TEXT NOT NULL)")
        self.db.commit()
        settings = json.dumps({"targets": self.targets, "reset_path": self.reset_path}, sort_keys=True)
        previous = self.db.execute("SELECT value FROM settings WHERE id=1").fetchone()
        if previous is not None and previous[0] != settings:
            self.db.close()
            self._lock.close()
            raise ValueError("Campaign store targets/reset hook differ; use original configuration or a new store")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO settings(id,value) VALUES(1,?)", (settings,))
        interrupted = self.db.execute("SELECT count(*) FROM jobs WHERE status='running'").fetchone()[0]
        needs_recovery = self.db.execute("SELECT required FROM recovery").fetchone()[0]
        if (interrupted or needs_recovery) and not recover_interrupted:
            self.db.close()
            self._lock.close()
            raise ValueError("Interrupted campaign requires operator recovery: stop orphan worker process trees, reset all targets, then enable recover_interrupted")
        with self.db:
            self.db.execute("UPDATE recovery SET required=0")
            self.db.execute("UPDATE jobs SET status='inconclusive', reason='Service interrupted; inspect and reset target before reuse' WHERE status='running'")
        self.timeout = timeout
        self.tasks: dict[str, asyncio.Task] = {}
        self._closed = False

    def _available_scenarios(self):
        approved = [BrowserScenario.model_validate_json(row[0]) for row in
                    self.db.execute("SELECT scenario FROM approved_discoveries a WHERE EXISTS (SELECT 1 FROM jobs j WHERE j.campaign=a.validation_campaign) AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.campaign=a.validation_campaign AND j.status != 'passed')")]
        return self.scenarios + approved

    def list_discovery_goals(self) -> list[dict]:
        return [goal.model_dump() for goal in self.discovery_goals]

    async def start_discovery(self, goal_name: str) -> dict:
        if self._closed:
            raise ValueError("Campaign service is closed")
        goal = next((goal for goal in self.discovery_goals if goal.name == goal_name), None)
        if goal is None:
            raise ValueError("Unknown discovery goal")
        if self.db.execute("SELECT count(*) FROM jobs").fetchone()[0] >= self.MAX_JOBS:
            raise ValueError("Store job limit reached; operator must archive store")
        campaign_id = uuid.uuid4().hex
        with self.db:
            self.db.execute("INSERT INTO campaigns(id) VALUES (?)", (campaign_id,))
            self.db.execute("INSERT INTO jobs(id,campaign,scenario,profile,snapshot,status,kind) VALUES (?,?,?,?,?,'queued','discovery')",
                            (uuid.uuid4().hex, campaign_id, goal.name, "discovery", goal.model_dump_json()))
        await self.resume()
        return self.get_discovery(campaign_id)

    def _discovery_job(self, campaign_id):
        rows = self.db.execute("SELECT * FROM jobs WHERE campaign=?", (campaign_id,)).fetchall()
        if len(rows) != 1 or rows[0]["kind"] != "discovery":
            raise ValueError("Unknown discovery campaign ID")
        return rows[0]

    def _validate_draft(self, job):
        if job["status"] != "proposed" or not job["draft"]:
            raise ValueError("Discovery has no proposed scenario to approve")
        scenario = BrowserScenario.model_validate_json(job["draft"])
        goal = json.loads(job["snapshot"])
        if (scenario.name != goal["name"] or scenario.goal != goal["goal"]
                or [a.model_dump() for a in scenario.assertions] != goal["assertions"]
                or len(scenario.steps) > goal["max_steps"]
                or scenario.steps[0].action != "goto" or scenario.steps[0].target != goal["start_path"]
                or scenario.profiles != [NetworkProfile(name="normal")]):
            raise ValueError("Draft does not preserve the configured goal, assertions, or discovery limits")
        return scenario

    def get_discovery(self, campaign_id: str) -> dict:
        job = self._discovery_job(campaign_id)
        result = self.get(campaign_id)
        result["goal_name"] = job["scenario"]
        result["requires_approval"] = job["status"] == "proposed"
        if job["status"] == "proposed":
            result["scenario"] = self._validate_draft(job).model_dump()
        approval = self.db.execute("SELECT validation_campaign FROM approved_discoveries WHERE source=?", (campaign_id,)).fetchone()
        if approval:
            result["requires_approval"] = False
            result["validation_campaign_id"] = approval[0]
            result["validation_status"] = self.get(approval[0])["status"]
        return result

    async def approve_discovery(self, campaign_id: str) -> dict:
        if self._closed:
            raise ValueError("Campaign service is closed")
        approval = self.db.execute("SELECT validation_campaign FROM approved_discoveries WHERE source=?", (campaign_id,)).fetchone()
        if approval:
            return self.get(approval[0])
        scenario = self._validate_draft(self._discovery_job(campaign_id))
        if (any(s.name == scenario.name for s in self.scenarios)
                or any(json.loads(row[0])["name"] == scenario.name for row in self.db.execute("SELECT scenario FROM approved_discoveries"))):
            raise ValueError("Scenario name already exists; configure a distinct discovery goal name")
        if self.db.execute("SELECT count(*) FROM jobs").fetchone()[0] >= self.MAX_JOBS:
            raise ValueError("Store job limit reached; operator must archive store")
        validation_id = uuid.uuid4().hex
        with self.db:
            self.db.execute("INSERT INTO campaigns(id) VALUES (?)", (validation_id,))
            self.db.execute("INSERT INTO jobs(id,campaign,scenario,profile,snapshot,status) VALUES (?,?,?,?,?,'queued')",
                            (uuid.uuid4().hex, validation_id, scenario.name, "normal", scenario.model_dump_json()))
            self.db.execute("INSERT INTO approved_discoveries VALUES (?,?,?)",
                            (campaign_id, scenario.model_dump_json(), validation_id))
        await self.resume()
        return self.get(validation_id)

    def list_scenarios(self) -> list[dict]:
        return [{"name": s.name, "goal": s.goal, "profiles": [p.name for p in s.profiles]}
                for s in self._available_scenarios()]

    async def start(self, scenarios: list[str]) -> dict:
        if self._closed:
            raise ValueError("Campaign service is closed")
        if not scenarios or len(scenarios) > 100 or len(set(scenarios)) != len(scenarios):
            raise ValueError("Select 1–100 distinct configured scenarios")
        configured = {s.name: s for s in self._available_scenarios()}
        if any(name not in configured for name in scenarios):
            raise ValueError("Unknown configured scenario")
        selected = [configured[name] for name in scenarios]
        count = sum(len(s.profiles) for s in selected)
        if count > self.MAX_CAMPAIGN_JOBS:
            raise ValueError("Campaign exceeds 100 scenario/profile jobs")
        if self.db.execute("SELECT count(*) FROM jobs").fetchone()[0] + count > self.MAX_JOBS:
            raise ValueError("Store job limit reached; operator must archive store")
        campaign_id = uuid.uuid4().hex
        with self.db:
            self.db.execute("INSERT INTO campaigns(id) VALUES (?)", (campaign_id,))
            for scenario in selected:
                for profile in scenario.profiles:
                    snapshot = scenario.model_copy(update={"profiles": [profile]}).model_dump_json()
                    self.db.execute("INSERT INTO jobs(id,campaign,scenario,profile,snapshot,status) VALUES (?,?,?,?,?,'queued')",
                                    (uuid.uuid4().hex, campaign_id, scenario.name, profile.name, snapshot))
        await self.resume()
        return self.get(campaign_id)

    async def resume(self):
        """Resume queued work only; call once inside the service lifespan."""
        if self._closed:
            raise ValueError("Campaign service is closed")
        for target in self.targets:
            if target not in self.tasks or self.tasks[target].done():
                self.tasks[target] = asyncio.create_task(self._worker(target))

    def get(self, campaign_id: str) -> dict:
        campaign = self.db.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
        if campaign is None:
            raise ValueError("Unknown campaign ID")
        rows = self.db.execute("SELECT id,scenario,profile,status,reason FROM jobs WHERE campaign=? ORDER BY seq", (campaign_id,)).fetchall()
        jobs = [dict(row) for row in rows]
        states = {j["status"] for j in jobs}
        status = ("cancelling" if campaign["cancelled"] and "running" in states else
                  "cancelled" if campaign["cancelled"] else
                  "running" if "running" in states else "queued" if "queued" in states else
                  "inconclusive" if states & {"inconclusive", "timed_out", "error"} else
                  "failed" if "failed" in states else "proposed" if states == {"proposed"} else "completed")
        return {"campaign_id": campaign_id, "status": status, "jobs": jobs}

    def _update(self, job_id: str, status: str, reason: str | None = None):
        with self.db:
            self.db.execute("UPDATE jobs SET status=?,reason=? WHERE id=?", (status, reason, job_id))

    async def _worker(self, target: str):
        while not self._closed:
            row = self.db.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY seq LIMIT 1").fetchone()
            if row is None:
                return
            self._update(row["id"], "running")
            asyncio.current_task().job_id = row["id"]
            try:
                status, reason = await self._execute(row, target)
                self._update(row["id"], status, reason)
            except asyncio.CancelledError:
                cancelled = self.db.execute("SELECT cancelled FROM campaigns WHERE id=?", (row["campaign"],)).fetchone()[0]
                if not cancelled:
                    with self.db:
                        self.db.execute("UPDATE recovery SET required=1")
                self._update(row["id"], "cancelled" if cancelled else "inconclusive", "Execution stopped; application writes are not undone")
                raise
            except Exception:  # noqa: BLE001 - isolate jobs and keep raw errors private
                self._update(row["id"], "error", "Worker could not complete; inspect local artifacts")

    async def _execute(self, job, target: str) -> tuple[str, str | None]:
        discovery = job["kind"] == "discovery"
        if discovery and not os.environ.get("ANTHROPIC_API_KEY"):
            return "inconclusive", "Discovery requires operator configuration of ANTHROPIC_API_KEY"
        if self.reset_path is not None:
            async with httpx.AsyncClient(follow_redirects=False, trust_env=False, timeout=10) as client:
                response = await client.post(target + self.reset_path)
                if not 200 <= response.status_code < 300:
                    return "error", "Target reset failed; browser execution skipped"
        folder = self.out / job["id"]
        folder.mkdir(mode=0o700)
        (folder / ("discovery.json" if discovery else "scenario.json")).write_text(job["snapshot"])
        env = {k: v for k, v in os.environ.items() if k in (
            "PATH", "HOME", "LANG", "LD_LIBRARY_PATH", "PLAYWRIGHT_BROWSERS_PATH")}
        if discovery:
            for key in ("ANTHROPIC_API_KEY",):
                if key in os.environ:
                    env[key] = os.environ[key]
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "feena.discovery_worker" if discovery else "feena.mcp_worker", str(folder), target,
                env=env, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True)
            await asyncio.wait_for(process.wait(), min(self.timeout, json.loads(job["snapshot"])["timeout_seconds"]) if discovery else self.timeout)
            if process.returncode:
                return "error", "Worker failed; inspect local artifacts"
            if discovery:
                result_path = folder / "discovery-results.json"
                if result_path.stat().st_size > 65536:
                    return "inconclusive", "Invalid discovery results"
                result = json.loads(result_path.read_text())
                if not isinstance(result, dict) or result.get("status") != "proposed":
                    return "inconclusive", "Discovery did not produce a reviewable scenario; inspect local artifacts"
                draft = json.dumps(result.get("scenario"))
                candidate = dict(job) | {"status": "proposed", "draft": draft}
                try:
                    self._validate_draft(candidate)
                except ValueError:
                    return "inconclusive", "Discovery draft failed validation"
                with self.db:
                    self.db.execute("UPDATE jobs SET draft=? WHERE id=?", (draft, job["id"]))
                return "proposed", "Review the generated scenario before approval and validation"
            result_path = folder / "results.json"
            if result_path.stat().st_size > 65536:
                return "error", "Invalid worker results"
            results = json.loads(result_path.read_text())
            if not isinstance(results, list) or len(results) != 1:
                return "error", "Invalid worker results"
            status = results[0].get("status")
            if status not in ("passed", "failed"):
                return "inconclusive", "Worker did not produce a definitive assertion result"
            return status, None  # Page content and raw failure reasons stay local.
        except TimeoutError:
            return "timed_out", "Server execution budget exceeded"
        finally:
            if process is not None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.wait()

    async def cancel(self, campaign_id: str) -> dict:
        current = self.get(campaign_id)
        if not any(job["status"] in ("queued", "running") for job in current["jobs"]):
            return current
        with self.db:
            self.db.execute("UPDATE campaigns SET cancelled=1 WHERE id=?", (campaign_id,))
            self.db.execute("UPDATE jobs SET status='cancelled' WHERE campaign=? AND status='queued'", (campaign_id,))
        # Only tasks currently executing this campaign are cancelled.
        running = {r[0] for r in self.db.execute("SELECT id FROM jobs WHERE campaign=? AND status='running'", (campaign_id,))}
        tasks = []
        for task in self.tasks.values():
            # Mapping is assigned by the worker before its first await.
            if getattr(task, "job_id", None) in running:
                if not task.cancelling():
                    task.cancel()
                tasks.append(task)
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.resume()
        return self.get(campaign_id)

    async def close(self):
        if self._closed:
            return
        self._closed = True
        for task in self.tasks.values():
            if not task.cancelling():
                task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        self.db.close()
        self._lock.close()
