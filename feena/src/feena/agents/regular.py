"""The regular agent: a new user honestly trying to use the app's core flows.

It looks for flows that fail end to end — signups that don't complete, buttons that do
nothing, checkouts that error — the failures a happy-path unit test never sees because it
mocks the pieces in between.
"""
from __future__ import annotations

from ..findings import Finding, Kind, Severity, Step
from .base import BaseAgent


class RegularAgent(BaseAgent):
    name = "regular"
    system = (
        "You are a first-time user of this web app. Try to accomplish the app's core jobs the "
        "way a real, well-intentioned person would: sign up, log in, do the main thing the app "
        "is for, and finish. You cannot see the source. Report a finding whenever a flow you "
        "reasonably expected to complete does not — a dead button, a step that errors, a form "
        "that never submits. Prefer finishing a whole flow over clicking around."
    )
    goal = "Complete the app's primary end-to-end flow as a new user."
    # Clean, goal-directed paths are what make good shortcuts for later runs.
    records_macros = True

    def evaluate(self, decision) -> None:
        page = self.ctx.session.page
        assert page is not None

        # A concrete signal a mock-based test misses: the user is looking at a 5xx or a
        # visible error boundary in the middle of a flow they were told would work.
        body = (page.inner_text("body") or "").lower() if page else ""
        looks_broken = any(
            marker in body
            for marker in ("500", "internal server error", "something went wrong", "unhandled")
        )
        if looks_broken:
            shot = self.ctx.session.screenshot(f"regular-{len(self.ctx.findings)}")
            self.ctx.findings.append(
                Finding(
                    kind=Kind.BROKEN_FLOW,
                    severity=Severity.HIGH,
                    title="Core flow hits a server error",
                    detail=(
                        "While completing a core user flow the page showed a server error / "
                        "error boundary. Happy-path tests that mock the backend would not catch "
                        "this."
                    ),
                    agent=self.name,
                    steps=[Step(action="goto", target=page.url, note="reached this URL in-flow")],
                    evidence={"screenshot": str(shot), "url": page.url},
                )
            )
