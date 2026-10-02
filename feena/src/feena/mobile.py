"""Experimental deterministic native journeys against a local Appium server."""
import argparse
import base64
import json
import os
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import quote, urlsplit

import httpx
import yaml
from pydantic import Field, model_validator

from .simulation_config import StrictModel
from .suites import write_summary


class MobileStep(StrictModel):
    action: Literal["tap", "fill", "wait"]
    using: Literal["accessibility id", "id", "xpath"] = "accessibility id"
    target: str = ""
    value: str = ""
    value_env: str | None = None
    wait_ms: int = Field(default=0, ge=0, le=30000)

    @model_validator(mode="after")
    def valid(self):
        if self.action != "wait" and not self.target:
            raise ValueError("Native interactions require a selector")
        if self.value_env and self.value:
            raise ValueError("Choose value or value_env")
        return self


class MobileAssertion(StrictModel):
    kind: Literal["visible", "text"]
    using: Literal["accessibility id", "id", "xpath"] = "accessibility id"
    target: str = Field(min_length=1)
    expected: str = ""


class MobileJourney(StrictModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    steps: list[MobileStep] = Field(min_length=1, max_length=100)
    assertions: list[MobileAssertion] = Field(min_length=1, max_length=50)


class MobileConfig(StrictModel):
    capabilities: dict = Field(min_length=1)
    journeys: list[MobileJourney] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def valid(self):
        if self.capabilities.get("platformName") not in ("Android", "iOS"):
            raise ValueError("Native platformName must be Android or iOS")
        if not self.capabilities.get("appium:automationName"):
            raise ValueError("Configure appium:automationName")
        if len({j.name for j in self.journeys}) != len(self.journeys):
            raise ValueError("Native journey names must be unique")
        return self


class DriverError(Exception):
    pass


class MissingElement(DriverError):
    pass


def run_journey(journey, capabilities, server, folder, client_factory=httpx.Client):
    parts = urlsplit(server)
    if (parts.scheme != "http" or parts.hostname not in ("localhost", "127.0.0.1", "::1")
            or parts.username or parts.password or parts.query or parts.fragment):
        raise ValueError("Use a local HTTP Appium server")
    folder.mkdir(parents=True, exist_ok=False)
    actions, session_id = [], None
    status, reason = "inconclusive", "Native journey did not complete"
    evidence_errors = []
    with client_factory(base_url=server.rstrip("/") + "/", timeout=60,
                        follow_redirects=False, trust_env=False) as client:
        def command(method, path, body=None):
            response = client.request(method, path, json=body)
            data = response.json().get("value")
            if isinstance(data, dict) and data.get("error") == "no such element":
                raise MissingElement()
            if response.status_code >= 300 or isinstance(data, dict) and data.get("error"):
                raise DriverError()
            return data

        def element(using, target):
            data = command("POST", f"session/{session_id}/element",
                           {"using": using, "value": target})
            return quote(data["element-6066-11e4-a52e-4f735466cecf"], safe="")

        try:
            # Resolve required credentials before creating a device session.
            values = [os.environ[s.value_env] if s.value_env else s.value for s in journey.steps]
            data = command("POST", "session", {"capabilities": {"alwaysMatch": capabilities}})
            session_id = quote(data["sessionId"], safe="")
            command("POST", f"session/{session_id}/timeouts", {"implicit": 3000})
            for index, (step, value) in enumerate(zip(journey.steps, values), 1):
                if step.action == "wait":
                    time.sleep(step.wait_ms / 1000)
                else:
                    eid = element(step.using, step.target)
                    suffix = "click" if step.action == "tap" else "value"
                    if step.action == "fill":
                        command("POST", f"session/{session_id}/element/{eid}/clear", {})
                    command("POST", f"session/{session_id}/element/{eid}/{suffix}",
                            {"text": value} if step.action == "fill" else {})
                actions.append({"step": index, "action": step.action, "status": "completed"})
            failures = []
            for index, assertion in enumerate(journey.assertions, 1):
                try:
                    eid = element(assertion.using, assertion.target)
                    actual = command("GET", f"session/{session_id}/element/{eid}/" +
                                     ("displayed" if assertion.kind == "visible" else "text"))
                    passed = actual is True if assertion.kind == "visible" else assertion.expected == actual
                except MissingElement:
                    passed = False
                if not passed:
                    failures.append(f"Assertion {index} ({assertion.kind}) did not match")
                actions.append({"assertion": index, "kind": assertion.kind,
                                "status": "passed" if passed else "failed"})
            status, reason = ("failed", "; ".join(failures)) if failures else ("passed", "")
        except Exception as exc:  # noqa: BLE001 - preserve inconclusive result and cleanup
            reason = f"Native setup/action failed: {type(exc).__name__}"
        finally:
            if session_id:
                try:
                    encoded = command("GET", f"session/{session_id}/screenshot")
                    screenshot = base64.b64decode(encoded, validate=True)
                    if not screenshot.startswith(b"\x89PNG\r\n\x1a\n"):
                        raise ValueError("Invalid screenshot")
                    (folder / "final.png").write_bytes(screenshot)
                except Exception:  # noqa: BLE001 - evidence failure must not skip cleanup
                    evidence_errors.append("Screenshot unavailable")
                try:
                    command("DELETE", f"session/{session_id}")
                except Exception:  # noqa: BLE001 - retain failed cleanup in evidence metadata
                    evidence_errors.append("Device session cleanup failed")
            if evidence_errors and status == "passed":
                status, reason = "inconclusive", "; ".join(evidence_errors)
    (folder / "actions.json").write_text(json.dumps(actions, indent=2) + "\n")
    # Secrets and selector values are deliberately omitted from native evidence metadata.
    (folder / "manifest.json").write_text(json.dumps({"version": 1, "runner": "appium",
        "journey": journey.name, "platform": capabilities["platformName"],
        "status": status, "reason": reason, "evidence_errors": evidence_errors}, indent=2) + "\n")
    return {"name": journey.name, "profile": "native", "status": status,
            "reason": reason, "artifacts": str(folder.resolve())}


def main():
    parser = argparse.ArgumentParser(description="Experimental native critical-flow runner")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--server", default="http://127.0.0.1:4723")
    parser.add_argument("--journey")
    parser.add_argument("--out", type=Path, default=Path(".feena/suite-runs"))
    args = parser.parse_args()
    cfg = MobileConfig.model_validate(yaml.safe_load(args.config.read_text()))
    selected = [j for j in cfg.journeys if args.journey is None or j.name == args.journey]
    if not selected:
        parser.error("Unknown native journey")
    folder = args.out.resolve() / uuid.uuid4().hex
    results = [run_journey(j, cfg.capabilities, args.server, folder / "evidence" / j.name)
               for j in selected]
    states = {r["status"] for r in results}
    status = "failed" if "failed" in states else "inconclusive" if "inconclusive" in states else "passed"
    write_summary({"version": 1, "created_at": datetime.now(UTC).isoformat(),
                   "suite": "native", "status": status, "complete": True,
                   "counts": {s: sum(r["status"] == s for r in results) for s in states},
                   "results": results}, folder)
    print(f"Native release check: {status}; summary: {folder / 'summary.json'}")
    raise SystemExit(0 if status == "passed" else 1)


if __name__ == "__main__":
    main()
