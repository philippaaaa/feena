"""Deterministic, isolated browser simulations with optional network fault profiles."""
# ruff: noqa: BLE001, S110
from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from playwright.sync_api import BrowserContext, Page, expect, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from .outcomes import _contains, _local_path


@dataclass
class SimulationResult:
    name: str
    profile: str
    status: str
    reason: str = ""
    artifacts: Path | None = None


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._") or "simulation"


def _matches(url: str, method: str, profile, base_origin: str) -> bool:
    parts = urlsplit(url)
    if (parts.scheme, parts.netloc) != base_origin:
        return False
    path_filter = getattr(profile, "path", "/") or "/"
    if not parts.path.startswith(path_filter):
        return False
    expected_method = getattr(profile, "method", None)
    return expected_method is None or method.upper() == expected_method


def _run_profile(scenario, profile, base_url: str, directory: Path) -> SimulationResult:
    """Run one profile; only ``new_tab`` and ``switch_tab`` change the active tab."""
    result = SimulationResult(scenario.name, profile.name, "inconclusive", artifacts=directory)
    base_origin = (urlsplit(base_url).scheme, urlsplit(base_url).netloc)
    context: BrowserContext | None = None
    browser = None
    playwright = None
    pages: dict[str, Page] = {}
    actions: list[dict[str, Any]] = []
    faults_applied = 0
    matches_seen = 0
    fault_errors: list[str] = []
    failure: str | None = None
    assertion_failures: list[str] = []
    trace_started = False
    artifact_errors: list[str] = []
    run_started = time.monotonic()
    try:
        directory.mkdir(parents=True, exist_ok=False)
        playwright = sync_playwright().start()
        browser = playwright.chromium.launch()
        context = browser.new_context(
            viewport={"width": scenario.viewport_width, "height": scenario.viewport_height},
            service_workers="block",
        )
        context.tracing.start(screenshots=True, snapshots=True, sources=True)
        trace_started = True

        def continue_local(route):
            # Browser routing does not intercept every redirect hop. Do not let a
            # redirect bypass the origin check or silently reach another service.
            response = route.fetch(max_redirects=0, timeout=scenario.timeout_ms)
            if 300 <= response.status < 400:
                route.abort()
                fault_errors.append("RedirectBlocked")
                return
            route.fulfill(response=response)

        def _route_request(route):
            nonlocal matches_seen, faults_applied
            request = route.request
            if (urlsplit(request.url).scheme, urlsplit(request.url).netloc) != base_origin:
                route.abort()
                return
            if getattr(profile, "effect", "normal") == "normal" or faults_applied:
                continue_local(route)
                return
            if not _matches(request.url, request.method, profile, base_origin):
                continue_local(route)
                return
            matches_seen += 1
            if matches_seen != getattr(profile, "occurrence", 1):
                continue_local(route)
                return
            fault_started = time.monotonic()
            fault = {"event": "fault", "effect": profile.effect,
                     "method": request.method, "path": urlsplit(request.url).path,
                     "occurrence": matches_seen, "started_at_ms": round(
                         (fault_started - run_started) * 1000, 3)}
            try:
                if profile.effect == "delay":
                    time.sleep(profile.delay_ms / 1000)
                    continue_local(route)
                elif profile.effect == "abort":
                    route.abort()
                elif profile.effect == "drop_response":
                    route.fetch(max_redirects=0)
                    route.abort()
                else:
                    continue_local(route)
                faults_applied += 1
                fault["event"] = "fault_applied"
                fault["status"] = "completed"
            except Exception as exc:
                fault["status"] = "error"
                fault["error_type"] = type(exc).__name__
                fault_errors.append(type(exc).__name__)
                try:
                    route.abort()
                except Exception:
                    pass
            fault["elapsed_ms"] = round((time.monotonic() - fault_started) * 1000, 3)
            actions.append(fault)

        def route_request(route):
            try:
                _route_request(route)
            except Exception as exc:
                error_type = type(exc).__name__
                fault_errors.append(error_type)
                actions.append({"event": "route_error", "error_type": error_type,
                                "elapsed_ms": round((time.monotonic() - run_started) * 1000, 3)})
                try:
                    route.abort()
                except Exception:
                    pass

        context.route("**/*", route_request)
        context.route_web_socket("**/*", lambda ws: ws.close())
        pages["main"] = context.new_page()
        pages["main"].set_default_timeout(scenario.timeout_ms)
        active_tab = "main"
        for index, step in enumerate(scenario.steps, 1):
            action = step.action
            page = pages[active_tab]
            action_started = time.monotonic()
            try:
                if action == "new_tab":
                    page = context.new_page()
                    page.set_default_timeout(scenario.timeout_ms)
                    pages[step.tab] = page
                    active_tab = step.tab
                elif action == "switch_tab":
                    if step.tab not in pages:
                        raise ValueError("requested tab does not exist")
                    page = pages[step.tab]
                    page.bring_to_front()
                    active_tab = step.tab
                elif action == "goto":
                    target = step.target or "/"
                    page.goto(base_url.rstrip("/") + _local_path(target),
                              timeout=scenario.timeout_ms)
                elif action == "click":
                    page.locator(step.target).click(timeout=scenario.timeout_ms)
                elif action == "fill":
                    page.locator(step.target).fill(step.value, timeout=scenario.timeout_ms)
                elif action == "press":
                    page.locator(step.target).press(step.value, timeout=scenario.timeout_ms)
                elif action == "back":
                    page.go_back(timeout=scenario.timeout_ms)
                elif action == "reload":
                    page.reload(timeout=scenario.timeout_ms)
                elif action == "offline":
                    context.set_offline(True)
                elif action == "online":
                    context.set_offline(False)
                elif action == "wait":
                    page.wait_for_timeout(step.wait_ms)
                else:
                    raise ValueError("unsupported action")
                actions.append({"step": index, "action": action, "tab": active_tab,
                                "status": "completed", "elapsed_ms": round(
                                    (time.monotonic() - action_started) * 1000, 3)})
            except PlaywrightTimeoutError:
                failure = f"action {index} ({action}) timed out"
                actions.append({"step": index, "action": action, "tab": active_tab,
                                "status": "timed_out", "elapsed_ms": round(
                                    (time.monotonic() - action_started) * 1000, 3)})
                break
            except Exception as exc:
                failure = f"action {index} ({action}) failed: {type(exc).__name__}"
                actions.append({"step": index, "action": action, "tab": active_tab,
                                "status": "error", "error_type": type(exc).__name__,
                                "elapsed_ms": round((time.monotonic() - action_started) * 1000, 3)})
                break

        if not failure:
            for index, assertion in enumerate(scenario.assertions, 1):
                assertion_started = time.monotonic()
                try:
                    if assertion.kind == "json":
                        target = _local_path(assertion.target)
                        response = context.request.get(
                            base_url.rstrip("/") + target,
                            timeout=scenario.timeout_ms,
                            max_redirects=0,
                        )
                        if response.status != assertion.status_code:
                            raise AssertionError(
                                f"expected HTTP {assertion.status_code}, got {response.status}"
                            )
                        try:
                            actual = response.json()
                        except (ValueError, TypeError):
                            raise AssertionError("response was not valid JSON") from None
                        if not _contains(actual, assertion.expected):
                            raise AssertionError("JSON did not contain expected subset")
                    else:
                        page = pages[active_tab]
                        locator = page.locator(assertion.target)
                        if assertion.kind == "visible":
                            locator.wait_for(state="visible", timeout=scenario.timeout_ms)
                        elif assertion.kind == "text":
                            expect(locator).to_contain_text(
                                str(assertion.expected), timeout=scenario.timeout_ms
                            )
                        elif assertion.kind == "count":
                            expect(locator).to_have_count(
                                assertion.expected, timeout=scenario.timeout_ms
                            )
                        else:
                            raise ValueError("unsupported assertion kind")
                    actions.append({"assertion": index, "kind": assertion.kind,
                                    "status": "passed", "elapsed_ms": round(
                                        (time.monotonic() - assertion_started) * 1000, 3)})
                except PlaywrightTimeoutError:
                    assertion_failures.append(f"assertion {index} ({assertion.kind}) timed out")
                    actions.append({"assertion": index, "kind": assertion.kind,
                                    "status": "timed_out", "elapsed_ms": round(
                                        (time.monotonic() - assertion_started) * 1000, 3)})
                except AssertionError:
                    assertion_failures.append(
                        f"assertion {index} ({assertion.kind}) did not match expected outcome"
                    )
                    actions.append({"assertion": index, "kind": assertion.kind,
                                    "status": "failed", "elapsed_ms": round(
                                        (time.monotonic() - assertion_started) * 1000, 3)})
                except Exception as exc:
                    failure = f"assertion {index} failed: {type(exc).__name__}"
                    actions.append({"assertion": index, "kind": assertion.kind,
                                    "status": "error", "error_type": type(exc).__name__,
                                    "elapsed_ms": round(
                                        (time.monotonic() - assertion_started) * 1000, 3)})
                    break

        if failure:
            result.status, result.reason = "inconclusive", failure
        elif fault_errors:
            result.status = "inconclusive"
            result.reason = f"network fault handler failed: {fault_errors[0]}"
        elif assertion_failures:
            result.status, result.reason = "failed", "; ".join(assertion_failures)
        elif getattr(profile, "effect", "normal") != "normal" and not faults_applied:
            result.status = "inconclusive"
            result.reason = "requested network fault was not triggered"
        else:
            result.status = "passed"
    except Exception as exc:
        result.status = "inconclusive"
        result.reason = f"simulation setup/runtime failed: {type(exc).__name__}"
    finally:
        for tab, page in pages.items():
            try:
                if not page.is_closed():
                    page.screenshot(path=str(directory / ("final.png" if tab == "main"
                                                          else f"final-{_safe_name(tab)}.png")),
                                    full_page=True, timeout=5000)
            except Exception as exc:
                artifact_errors.append(f"screenshot:{tab}:{type(exc).__name__}")
        if trace_started and context is not None:
            try:
                context.tracing.stop(path=str(directory / "trace.zip"))
            except Exception as exc:
                artifact_errors.append(f"trace:{type(exc).__name__}")
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
        if playwright is not None:
            try:
                playwright.stop()
            except Exception:
                pass
        if result.status == "passed":
            missing = [name for name in ("final.png", "trace.zip")
                       if not (directory / name).is_file()]
            if missing or artifact_errors:
                result.status = "inconclusive"
                result.reason = "required artifacts missing or incomplete"
        try:
            (directory / "actions.json").write_text(
                json.dumps(actions, indent=2) + "\n", encoding="utf-8"
            )
            manifest = {
                "version": 1,
                "created_at": datetime.now(UTC).isoformat(),
                "base_url": base_url,
                "scenario": scenario.model_dump(mode="json"),
                "profile": profile.model_dump(mode="json"),
                "fault_matches_seen": matches_seen,
                "fault_applied": faults_applied > 0,
                "fault_errors": fault_errors,
                "artifact_errors": artifact_errors,
                "elapsed_ms": round((time.monotonic() - run_started) * 1000, 3),
                "status": result.status,
                "reason": result.reason,
            }
            (directory / "manifest.json").write_text(
                json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
            )
        except Exception as exc:
            if result.status == "passed":
                result.status = "inconclusive"
                result.reason = f"artifact writing failed: {type(exc).__name__}"
    return result


def run_simulations(scenarios, base_url: str, out_dir: Path) -> list[SimulationResult]:
    """Run each scenario/profile once; each gets a unique directory and browser context."""
    out_dir = Path(out_dir)
    results: list[SimulationResult] = []
    run_id = uuid.uuid4().hex
    for scenario in scenarios:
        for profile in scenario.profiles:
            directory = out_dir / run_id / _safe_name(scenario.name) / _safe_name(profile.name)
            results.append(_run_profile(scenario, profile, base_url, directory))
    return results


def render_simulations(results: list[SimulationResult]) -> str:
    if not results:
        return ""
    lines = ["### Browser simulations", ""]
    for result in results:
        reason = f" — {result.reason}" if result.reason else ""
        artifacts = f" ([artifacts]({result.artifacts}))" if result.artifacts else ""
        lines.append(
            f"- **{result.status.upper()}** `{result.name}` / `{result.profile}`"
            f"{reason}{artifacts}"
        )
    return "\n".join(lines)
