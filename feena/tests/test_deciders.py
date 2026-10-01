from types import SimpleNamespace

import pytest

from feena.llm import LLM, Decision, DecisionRequest, TieredDecider, parse


class Fixed:
    available = True
    name = "fixed"

    def __init__(self, result):
        self.result = result
        self.calls = 0

    def decide(self, request):
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.mark.parametrize("result", [
    Decision("click", target="99", confidence=.99),
    Decision("goto", target="https://external.example", confidence=.99),
    Decision("done", confidence=.99, raw={"action": "unsupported"}),
    Decision("fill", target="0", value_kind="unknown", confidence=.99),
    Decision("done", confidence=float("nan")),
    RuntimeError("provider failed"),
])
def test_invalid_fast_decisions_escalate(result):
    smart = Fixed(Decision("done"))
    decider = TieredDecider(Fixed(result), smart)
    request = DecisionRequest("system", "goal", "snapshot", candidates=['button "Save"'])
    assert decider.decide(request).action == "done"
    assert smart.calls == 1


@pytest.mark.parametrize("confidence", ['NaN', 'Infinity', '-Infinity', 'true'])
def test_nonfinite_or_boolean_confidence_is_not_trusted(confidence):
    assert parse('{"action":"done","confidence":' + confidence + '}').confidence is None


def test_legacy_discovery_request_preserves_selectors():
    model = LLM()
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=
            '{"action":"click","target":"#checkout"}')])

    model._client = SimpleNamespace(messages=SimpleNamespace(create=create))
    result = model.decide("system", "checkout", "button", [])
    assert result.target == "#checkout"
    assert result.raw["action"] == "click"
    assert "Playwright selector" in calls[0]["messages"][0]["content"]


def test_structured_request_uses_numbered_menu():
    model = LLM()
    calls = []
    model._client = SimpleNamespace(messages=SimpleNamespace(create=lambda **kwargs:
        calls.append(kwargs) or SimpleNamespace(content=[SimpleNamespace(type="text", text=
            '{"action":"click","target":"0"}')])) )
    result = model.decide(DecisionRequest("system", "checkout", "page", candidates=['[0] Save']))
    assert result.target_index == 0
    assert "[0] Save" in calls[0]["messages"][0]["content"]
