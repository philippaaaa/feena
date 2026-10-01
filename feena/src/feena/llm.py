"""The models the exploratory agents decide with.

Every step is one *decision*: given the goal, the page, and a numbered menu of candidates
(elements, plus any memoized macros), pick an action and a target number. Keeping the answer
fixed-shape means the decider is swappable:

* :class:`ClaudeDecider` (default) asks a Claude model for strict JSON.
* :class:`TieredDecider` asks a cheap ``fast`` decider first and escalates to the ``smart`` one
  when the fast answer is low-confidence, malformed, or an action the fast model shouldn't own
  (writing a finding). This is the seam for a System-1 decision model such as Jev: implement
  :class:`Decider` (return ``confidence``), set it as ``fast``, keep Claude as ``smart``.

If no API key is present, ``available`` is False and the deterministic parts of Feena (hostile
checks, macro replay, outcomes, simulations) still run.
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from typing import Protocol

MODEL = "claude-sonnet-4-6"

ACTIONS = ("click", "dblclick", "fill", "press", "goto", "back", "macro", "done", "note_finding")


@dataclass
class Decision:
    action: str               # one of ACTIONS
    target: str = ""          # candidate number (click/dblclick/fill), macro number, path, or key
    value: str = ""           # literal text to type (generative deciders only)
    reason: str = ""
    raw: dict | None = None
    value_kind: str = ""      # synthetic value or configured credential reference
    confidence: float | None = None
    decided_by: str = ""

    @property
    def target_index(self) -> int | None:
        try:
            return int(str(self.target).strip().lstrip("[#M").rstrip("]"))
        except (TypeError, ValueError):
            return None


@dataclass
class DecisionRequest:
    system: str
    goal: str
    snapshot: str
    candidates: list[str] = field(default_factory=list)   # rendered Candidate.label()s
    macros: list[str] = field(default_factory=list)       # rendered "M<i> reach /x as alice ..."
    history: list[str] = field(default_factory=list)
    value_kinds: list[str] = field(default_factory=list)


class Decider(Protocol):
    name: str

    @property
    def available(self) -> bool: ...

    def decide(self, req: DecisionRequest) -> Decision: ...


def render_prompt(req: DecisionRequest) -> str:
    parts = [f"Goal: {req.goal}", ""]
    parts += ["Recent actions:", *(req.history[-8:] or ["(none yet)"]), ""]
    parts += ["Current page (accessibility tree):", req.snapshot or "(empty)", ""]
    parts += ["Elements you can act on (use the number as target):",
              *(req.candidates or ["(none)"]), ""]
    if req.macros:
        parts += [("Recorded shortcuts (action \"macro\", target = the M number). They replay a "
                  "known-good path without thinking; use one when it gets you where you need to "
                  "be faster:"), *req.macros, ""]
    if req.value_kinds:
        parts += ["For fill, set value_kind to one of: " + ", ".join(req.value_kinds)
                  + ". userN_email/userN_password log in as seeded account N. You may instead "
                  "give a literal value.", ""]
    parts.append(
        "Respond with ONLY a JSON object: {\"action\": \"" + "|".join(ACTIONS) + "\", "
        "\"target\": \"<number, path for goto, or key for press>\", \"value\": \"\", "
        "\"value_kind\": \"\", \"reason\": \"...\", \"confidence\": 0.0-1.0}. "
        "No prose, no markdown fences.")
    return "\n".join(parts)


class ClaudeDecider:
    def __init__(self, model: str = MODEL, *, timeout: float = 30.0, max_retries: int = 0):
        self.model = model
        self.name = f"claude:{model}"
        self._client = None
        key = os.environ.get("ANTHROPIC_API_KEY")
        if key:
            try:
                import anthropic

                self._client = anthropic.Anthropic(api_key=key, timeout=timeout, max_retries=max_retries)
            except Exception:  # noqa: BLE001 - unavailable provider disables exploration
                self._client = None

    @property
    def available(self) -> bool:
        return self._client is not None

    def decide(self, req: DecisionRequest | str, goal: str | None = None,
               snapshot: str | None = None, history: list[str] | None = None) -> Decision:
        # Discovery still chooses selectors through the legacy four-argument API.
        legacy = isinstance(req, str)
        if legacy:
            req = DecisionRequest(system=req, goal=goal or "", snapshot=snapshot or "",
                                  history=history or [])
        if not self.available:
            return Decision(action="done", reason="no LLM configured", decided_by=self.name)
        msg = self._client.messages.create(
            model=self.model,
            max_tokens=512,
            system=req.system,
            messages=[{"role": "user", "content": (
                f"Goal: {req.goal}\nRecent actions: " + "\n".join(req.history[-8:])
                + f"\nCurrent page:\n{req.snapshot}\nRespond with ONLY JSON: "
                '{"action":"click|fill|goto|done","target":"Playwright selector or local path",'
                '"value":"","reason":""}'
                if legacy else render_prompt(req))}],
        )
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        d = parse(text)
        d.decided_by = self.name
        return d


class TieredDecider:
    """Fast decider first; escalate to the smart one when it isn't sure."""

    ESCALATE_ACTIONS = frozenset({"note_finding"})

    def __init__(self, fast: Decider, smart: Decider, escalate_below: float = 0.6):
        self.fast = fast
        self.smart = smart
        self.escalate_below = escalate_below
        self.name = f"tiered({fast.name}->{smart.name})"
        self.stats = {"fast": 0, "escalated": 0}

    @property
    def available(self) -> bool:
        return self.fast.available or self.smart.available

    def decide(self, req: DecisionRequest) -> Decision:
        if self.fast.available:
            try:
                d = self.fast.decide(req)
                confidence = d.confidence
                confident = (type(confidence) in (float, int) and math.isfinite(confidence)
                             and confidence >= self.escalate_below)
                valid = isinstance(d, Decision) and d.action in ACTIONS
                if d.raw is not None and d.raw.get("action") not in ACTIONS:
                    valid = False
                if req is not None and d.action in {"click", "dblclick", "fill", "macro"}:
                    count = len(req.macros if d.action == "macro" else req.candidates)
                    valid = valid and d.target_index is not None and 0 <= d.target_index < count
                if req is not None and d.action == "fill" and d.value_kind:
                    valid = valid and d.value_kind in req.value_kinds
                if d.action == "goto":
                    from .simulation_config import local_path
                    local_path(d.target)
                if confident and valid and d.action not in self.ESCALATE_ACTIONS:
                    self.stats["fast"] += 1
                    return d
            except Exception:  # noqa: BLE001, S110 - failed fast providers escalate without leaking errors
                pass
        if not self.smart.available:
            return Decision(action="done", reason="no smart decider configured")
        self.stats["escalated"] += 1
        return self.smart.decide(req)


# Back-compat name: older code constructs ``LLM()``.
LLM = ClaudeDecider


def make_decider(models_cfg=None) -> Decider:
    """Build the decider described by ``feena.yaml``'s ``models`` block."""
    smart = ClaudeDecider(getattr(models_cfg, "smart", None) or MODEL)
    fast_name = getattr(models_cfg, "fast", None)
    if not fast_name:
        return smart
    if fast_name.startswith("claude"):
        fast: Decider = ClaudeDecider(fast_name)
    else:
        from .plugins import load_decider
        fast = load_decider(fast_name)
    return TieredDecider(fast, smart, getattr(models_cfg, "escalate_below", 0.6))


def parse(text: str) -> Decision:
    text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        d = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return Decision(action="done", reason="unparseable model output")
    if not isinstance(d, dict):
        return Decision(action="done", reason="unparseable model output")
    conf = d.get("confidence")
    try:
        conf = None if conf is None or isinstance(conf, bool) else float(conf)
        if conf is not None:
            conf = max(0.0, min(1.0, conf)) if math.isfinite(conf) else None
    except (TypeError, ValueError):
        conf = None
    if conf is not None and not math.isfinite(conf):
        conf = None
    action = str(d.get("action", "done"))
    return Decision(
        action=action if action in ACTIONS else "done",
        target=str(d.get("target", "")),
        value=str(d.get("value", "") or ""),
        value_kind=str(d.get("value_kind", "") or ""),
        reason=str(d.get("reason", "")),
        confidence=conf,
        raw=d,
    )


# Compatibility for integrations using the previous parser name.
_parse = parse
