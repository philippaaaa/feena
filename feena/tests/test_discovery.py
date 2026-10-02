from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from feena.discovery import _same_origin, run_discovery
from feena.discovery_config import DiscoveryGoal
from feena.llm import Decision


def goal(**kwargs):
    return DiscoveryGoal(name="signup", goal="Submit the form", assertions=[
        {"kind": "visible", "target": "#success"}], **kwargs)


@pytest.mark.parametrize("kwargs", [{"start_path": "//evil.test"}, {"max_steps": 51},
                                   {"timeout_seconds": 121}, {"unknown": 1}])
def test_goal_bounds(kwargs):
    with pytest.raises(ValidationError):
        goal(**kwargs)


def test_origin_boundary():
    origin = ("https", "example.test", 443)
    assert _same_origin("https://example.test:443/path", origin)
    assert not _same_origin("https://example.test.evil/path", origin)
    assert not _same_origin("https://user@example.test/path", origin)
    assert not _same_origin("file:///etc/passwd", origin)


class Model:
    available = True

    def __init__(self, decisions):
        self.decisions = iter(decisions)

    def decide(self, *args):
        return next(self.decisions)


def decision(action, target="", value=""):
    return Decision(action, target, value, raw={"action": action})


@pytest.fixture
def browser(monkeypatch):
    class Page:
        url = "https://test.local/"
        fail_assertion = False

        def goto(self, url, **kwargs): self.url = url
        def locator(self, selector): return self
        def evaluate(self, expression): return []
        def aria_snapshot(self): return '- button "Submit"'
        def click(self): pass
        def fill(self, value): pass
        def wait_for(self, **kwargs):
            if self.fail_assertion:
                raise ValueError("secret page content")
        def screenshot(self, **kwargs): pass
        def set_default_timeout(self, timeout): pass

    page = Page()
    class Context:
        tracing = SimpleNamespace(start=lambda **kw: None, stop=lambda **kw: None)
        def new_page(self): return page
        def route(self, *args): pass
        def route_web_socket(self, *args): pass
        def close(self): pass

    browser = SimpleNamespace(new_context=lambda **kw: Context(), close=lambda: None)
    @contextmanager
    def factory():
        yield SimpleNamespace(chromium=SimpleNamespace(launch=lambda **kw: browser))
    monkeypatch.setattr("feena.discovery.sync_playwright", factory)
    return page


def test_proposal_preserves_operator_assertions(browser, tmp_path):
    config = goal()
    result = run_discovery(config, "https://test.local", tmp_path,
                           Model([decision("click", "button"), decision("done")]))
    assert result["status"] == "proposed"
    assert result["scenario"]["assertions"] == [a.model_dump() for a in config.assertions]
    assert [s["action"] for s in result["scenario"]["steps"]] == ["goto", "click"]


@pytest.mark.parametrize("decisions", [[decision("done")], [Decision("done")],
    [decision("goto", "https://evil.test")], [decision("note_finding")]])
def test_bad_or_empty_journeys_inconclusive(browser, tmp_path, decisions):
    result = run_discovery(goal(), "https://test.local", tmp_path, Model(decisions))
    assert result["status"] == "inconclusive"
    assert "scenario" not in result


def test_assertions_must_pass(browser, tmp_path):
    browser.fail_assertion = True
    result = run_discovery(goal(), "https://test.local", tmp_path,
                           Model([decision("click", "button"), decision("done")]))
    assert result["status"] == "inconclusive"
    assert "secret" not in str(result)


def test_budget_counts_initial_navigation(browser, tmp_path):
    result = run_discovery(goal(max_steps=1), "https://test.local", tmp_path,
                           Model([decision("click", "button")]))
    assert result["status"] == "inconclusive"


def test_no_provider(tmp_path):
    assert run_discovery(goal(), "https://test.local", tmp_path,
                         SimpleNamespace(available=False))["status"] == "inconclusive"


def test_model_exception_is_private(browser, tmp_path):
    class BrokenModel:
        available = True
        def decide(self, *args):
            raise RuntimeError("provider secret-key and page data")
    result = run_discovery(goal(), "https://test.local", tmp_path, BrokenModel())
    assert result["status"] == "inconclusive"
    assert "secret-key" not in str(result)


def test_request_route_rejects_cross_origin(monkeypatch, tmp_path):
    # The production route predicate is also applied to redirects/subresources.
    assert not _same_origin("https://test.local@evil.test/x", ("https", "test.local", 443))
    assert not _same_origin("http://test.local/x", ("https", "test.local", 443))


def test_route_does_not_follow_redirects():
    from unittest.mock import Mock

    from feena.discovery import _route_request
    route = Mock()
    route.request.url = "https://test.local/redirect"
    route.fetch.return_value.status = 200
    _route_request(route, ("https", "test.local", 443))
    route.fetch.assert_called_once_with(max_redirects=0, timeout=5000)
    route.fulfill.assert_called_once_with(response=route.fetch.return_value)
    route.continue_.assert_not_called()
    route.request.url = "https://evil.local/destination"
    route.reset_mock()
    _route_request(route, ("https", "test.local", 443))
    route.abort.assert_called_once()
    route.fetch.assert_not_called()


def test_redirect_response_is_aborted():
    from unittest.mock import Mock

    from feena.discovery import _route_request
    route = Mock()
    route.request.url = "https://test.local/redirect"
    route.fetch.return_value.status = 302
    _route_request(route, ("https", "test.local", 443))
    route.abort.assert_called_once()
    route.fulfill.assert_not_called()
