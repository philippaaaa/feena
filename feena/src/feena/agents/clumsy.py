"""The clumsy agent: a distracted, imperfect user.

It does the things real people do and tests never do: double-clicks submit, pastes a novel
into a name field, hits back mid-flow, opens a second tab, fills forms out of order. These
surface state bugs, race conditions, validation gaps and data loss.
"""
from __future__ import annotations

from ..findings import Finding, Kind, Severity, Step
from .base import BaseAgent


class ClumsyAgent(BaseAgent):
    name = "clumsy"
    system = (
        "You are a distracted, error-prone user. You are not malicious, just messy. Do things "
        "real people do that break apps: double-click the submit button, paste very long or "
        "empty or emoji-only values into fields, press the browser back button in the middle "
        "of a multi-step flow, submit forms with fields filled in the wrong order. Report a "
        "finding when the app loses data, double-processes an action, shows a broken state, or "
        "accepts input it should have rejected."
    )
    goal = "Stress the app's forms and flows the way a careless real user would."

    # Uses shortcuts to get somewhere quickly, but never records: its variation is the point.
    records_macros = False

    def act(self, decision, candidates=None):
        # Add a little chaos on top of the model's chosen action: when the model says it wants
        # a double click, probe for double-submit handling even if it answered "click".
        if decision.action == "click" and decision.reason and "double" in decision.reason.lower():
            decision.action = "dblclick"
        return super().act(decision, candidates)

    def evaluate(self, decision) -> None:
        page = self.ctx.session.page
        assert page is not None
        body = (page.inner_text("body") or "").lower() if page else ""
        # A cheap heuristic: an unhandled exception surfaced to the user after messy input.
        if any(m in body for m in ("traceback", "unhandled", "cannot read properties of")):
            shot = self.ctx.session.screenshot(f"clumsy-{len(self.ctx.findings)}")
            self.ctx.findings.append(
                Finding(
                    kind=Kind.STATE_BUG,
                    severity=Severity.MEDIUM,
                    title="Messy input surfaces an unhandled error",
                    detail=(
                        "Careless-but-normal user behaviour produced an unhandled error visible "
                        "to the user. Indicates a missing validation or state-handling gap."
                    ),
                    agent=self.name,
                    steps=[Step(action=decision.action, target=self.target_for_step(decision),
                                value=decision.value_kind or decision.value,
                                note=decision.reason)],
                    evidence={"screenshot": str(shot), "url": page.url},
                )
            )
