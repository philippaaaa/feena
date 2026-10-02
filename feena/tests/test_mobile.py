import base64
import json

import httpx
import pytest

from feena.mobile import MobileConfig, MobileJourney, run_journey


def journey():
    return MobileJourney(name="login", steps=[{"action": "fill", "target": "email",
                        "value_env": "FEENA_TEST_EMAIL"}, {"action": "tap", "target": "login"}],
                         assertions=[{"kind": "text", "target": "welcome", "expected": "Welcome"}])


@pytest.mark.parametrize("actual,expected", [("Welcome", "passed"), ("Wrong", "failed")])
def test_native_protocol_assertions_cleanup_and_private_metadata(tmp_path, monkeypatch, actual, expected):
    monkeypatch.setenv("FEENA_TEST_EMAIL", "private@example.test")
    calls = []

    def respond(request):
        calls.append(request)
        path = request.url.path
        value = None
        if path == "/session":
            value = {"sessionId": "session1"}
        elif path.endswith("/element"):
            value = {"element-6066-11e4-a52e-4f735466cecf": "element1"}
        elif path.endswith("/text"):
            value = actual
        elif path.endswith("/screenshot"):
            value = base64.b64encode(b"\x89PNG\r\n\x1a\nexample").decode()
        return httpx.Response(200, json={"value": value})

    def factory(**kwargs):
        return httpx.Client(transport=httpx.MockTransport(respond), **kwargs)

    folder = tmp_path / "run"
    result = run_journey(journey(), {"platformName": "Android"}, "http://127.0.0.1:4723", folder, factory)
    assert result["status"] == expected
    assert calls[-1].method == "DELETE" and calls[-1].url.path == "/session/session1"
    assert "private@example.test" not in (folder / "manifest.json").read_text()
    assert "private@example.test" not in (folder / "actions.json").read_text()
    assert json.loads(calls[0].content)["capabilities"]["alwaysMatch"]["platformName"] == "Android"


def test_missing_credentials_never_open_device_session(tmp_path, monkeypatch):
    monkeypatch.delenv("FEENA_TEST_EMAIL", raising=False)
    calls = []
    def factory(**kwargs):
        return httpx.Client(transport=httpx.MockTransport(lambda r: calls.append(r)), **kwargs)
    result = run_journey(journey(), {"platformName": "iOS"}, "http://localhost:4723", tmp_path / "run", factory)
    assert result["status"] == "inconclusive" and not calls


def test_remote_appium_and_invalid_native_specs_rejected(tmp_path):
    with pytest.raises(ValueError):
        run_journey(journey(), {}, "http://example.com:4723", tmp_path / "run")
    with pytest.raises(ValueError):
        MobileConfig(capabilities={"platformName": "web"}, journeys=[journey()])


@pytest.mark.parametrize("failure", ["action", "screenshot", "cleanup"])
def test_native_errors_never_pass_and_cleanup_is_attempted(tmp_path, monkeypatch, failure):
    monkeypatch.setenv("FEENA_TEST_EMAIL", "test@example.test")
    calls = []
    def respond(request):
        calls.append(request)
        path = request.url.path
        if ((failure == "action" and path.endswith("/click")) or
                (failure == "screenshot" and path.endswith("/screenshot")) or
                (failure == "cleanup" and request.method == "DELETE")):
            return httpx.Response(500, json={"value": {"error": "unknown error"}})
        value = None
        if path == "/session":
            value = {"sessionId": "session1"}
        elif path.endswith("/element"):
            value = {"element-6066-11e4-a52e-4f735466cecf": "element1"}
        elif path.endswith("/text"):
            value = "Welcome"
        elif path.endswith("/screenshot"):
            value = base64.b64encode(b"\x89PNG\r\n\x1a\nexample").decode()
        return httpx.Response(200, json={"value": value})
    def factory(**kwargs):
        return httpx.Client(transport=httpx.MockTransport(respond), **kwargs)
    result = run_journey(journey(), {"platformName": "Android"}, "http://localhost:4723",
                         tmp_path / "run", factory)
    assert result["status"] == "inconclusive"
    assert calls[-1].method == "DELETE"
