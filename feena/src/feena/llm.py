"""Thin wrapper around the model the exploratory agents reason with.

Kept deliberately small: agents ask for the next action given a page snapshot and a goal, and
get back a structured decision. If no API key is present, ``available`` is False and the
deterministic parts of Feena (the hostile checks) still run.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass

MODEL = "claude-sonnet-4-6"


@dataclass
class Decision:
    action: str        # "click" | "fill" | "goto" | "done" | "note_finding"
    target: str = ""
    value: str = ""
    reason: str = ""
    raw: dict | None = None


class LLM:
    def __init__(self, model: str = MODEL, *, timeout: float = 30.0, max_retries: int = 0):
        self.model = model
        self._client = None
        key = os.environ.get("ANTHROPIC_API_KEY")
        if key:
            try:
                import anthropic

                self._client = anthropic.Anthropic(api_key=key, timeout=timeout, max_retries=max_retries)
            except Exception:  # noqa: BLE001 - unavailable providers disable exploration
                self._client = None

    @property
    def available(self) -> bool:
        return self._client is not None

    def decide(self, system: str, goal: str, snapshot: str, history: list[str]) -> Decision:
        """Ask for the next action as strict JSON. Falls back to 'done' if unavailable."""
        if not self.available:
            return Decision(action="done", reason="no LLM configured")

        prompt = (
            f"Goal: {goal}\n\n"
            f"Recent actions:\n" + "\n".join(history[-8:]) + "\n\n"
            f"Current page (accessibility tree):\n{snapshot}\n\n"
            "Respond with ONLY a JSON object: "
            '{"action": "click|fill|goto|done|note_finding", "target": "...", '
            '"value": "...", "reason": "..."}. No prose, no markdown fences.'
        )
        msg = self._client.messages.create(
            model=self.model,
            max_tokens=512,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        return _parse(text)


def _parse(text: str) -> Decision:
    text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        d = json.loads(text)
        return Decision(
            action=d.get("action", "done"),
            target=d.get("target", ""),
            value=d.get("value", ""),
            reason=d.get("reason", ""),
            raw=d,
        )
    except json.JSONDecodeError:
        return Decision(action="done", reason="unparseable model output")
