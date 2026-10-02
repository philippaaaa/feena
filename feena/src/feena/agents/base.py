"""The shared agent loop: perceive -> decide -> act -> evaluate.

Exploratory agents (regular, clumsy) subclass this and supply a system prompt and a goal. The
hostile agent does not use the LLM loop for its checks; it subclasses to reuse setup only.

Each step the decider sees the accessibility tree plus a numbered menu of actionable elements
(and any promoted macros) and picks one by number; see ``feena.actions`` and ``feena.llm``.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .. import values
from ..actions import Candidate, extract_candidates, fingerprint
from ..browser import scoped_url
from ..config import Config
from ..findings import Finding
from ..llm import LLM, Decider, Decision, DecisionRequest  # noqa: F401 - LLM: old import path
from ..macros import Macro, MacroStep, MacroStore, TraceRecorder, replay


@dataclass
class AgentContext:
    cfg: Config
    session: object                     # feena.browser.Session
    llm: Decider
    history: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    macros: MacroStore | None = None
    focus_hint: str = ""                # from blast radius: URLs only, never source
    avoid_routes: list[str] = field(default_factory=list)   # macro invalidation for this run
    stats: dict = field(default_factory=lambda: {
        "decisions": 0, "macro_replays": 0, "macro_steps_saved": 0, "macro_failures": 0,
        "macros_recorded": 0})


class BaseAgent:
    name: str = "base"
    system: str = ""
    goal: str = ""
    records_macros: bool = False   # only clean, goal-directed agents should teach shortcuts
    uses_macros: bool = True

    def __init__(self, ctx: AgentContext):
        self.ctx = ctx
        self._last_selector = ""
        self.recorder: TraceRecorder | None = None

    # ------------------------------------------------------------------ loop

    def run(self) -> list[Finding]:
        """Default: model-driven exploration under a time and step budget."""
        ctx = self.ctx
        cfg = ctx.cfg.run
        deadline = time.time() + cfg.budget_seconds
        ctx.session.goto("/")
        if not ctx.llm.available:
            return ctx.findings   # nothing to decide with; deterministic checks run elsewhere
        if self.records_macros and ctx.macros is not None:
            self.recorder = TraceRecorder("/", source=self.name)
        offered: list[Macro] = []
        if self.uses_macros and ctx.macros is not None:
            offered = ctx.macros.offerable(avoid_routes=ctx.avoid_routes)
        goal = f"{self.goal} {ctx.focus_hint}".strip()
        kinds = values.all_kinds(len(ctx.cfg.users))

        for _ in range(cfg.max_steps):
            if time.time() > deadline:
                break
            page = ctx.session.page
            candidates = extract_candidates(page)
            req = DecisionRequest(
                system=self._redact(self.system), goal=self._redact(goal), snapshot=self._redact(ctx.session.snapshot()),
                candidates=[self._redact(c.label()) for c in candidates],
                macros=[f"[M{i}] {m.describe()}" for i, m in enumerate(offered)],
                history=[self._redact(line) for line in ctx.history], value_kinds=kinds)
            decision = ctx.llm.decide(req)
            ctx.stats["decisions"] += 1
            ctx.history.append(f"{decision.action} {self._describe(decision, candidates)} "
                               f":: {decision.reason}")

            if decision.action == "done":
                break
            if decision.action == "macro":
                self._use_macro(decision, offered)
            else:
                ok, step = self.act(decision, candidates)
                if self.recorder is not None and step is not None:
                    try:
                        fp = fingerprint(page)
                    except Exception:  # noqa: BLE001 - unavailable structure stops recording
                        fp = ""
                    self.recorder.record(step, ok, self._path(), fp)
            ctx.history[:] = [self._redact(line) for line in ctx.history]
            self.evaluate(decision)

        self._flush_macros()
        return ctx.findings

    # ------------------------------------------------------------------ actions

    def act(self, decision: Decision, candidates: list[Candidate] | None = None
            ) -> tuple[bool, MacroStep | None]:
        """Perform one action. Returns (succeeded, replayable step or None)."""
        page = self.ctx.session.page
        assert page is not None
        candidates = candidates or []
        try:
            if decision.action == "goto":
                url = scoped_url(self.ctx.session.base_url, decision.target or "/")
                parts = urlsplit(url)
                path = parts.path + ("?" + parts.query if parts.query else "")
                self.ctx.session.goto(path)
                self._last_selector = path
                return True, MacroStep(action="goto", path=path)
            if decision.action == "back":
                page.go_back()
                return True, MacroStep(action="back")
            if decision.action == "press":
                key = decision.target or "Enter"
                page.keyboard.press(key)
                return True, MacroStep(action="press", key=key)
            if decision.action not in ("click", "dblclick", "fill"):
                return False, None

            idx = decision.target_index
            if idx is None:
                # A decider that still answers with a raw selector: run it, but don't memoize.
                if not decision.target:
                    return False, None
                self.recorder = None
                self._last_selector = decision.target
                getattr(page, decision.action)(decision.target, *(
                    [self._value(decision)[0]] if decision.action == "fill" else []),
                    timeout=3000)
                _settle(page)
                return True, None
            if not 0 <= idx < len(candidates):
                self.ctx.history.append(f"action failed: no element [{idx}] on this page")
                return False, None
            cand = candidates[idx]
            self._last_selector = cand.selector()
            loc = cand.locator(page)
            if decision.action == "fill":
                text, kind = self._value(decision)
                loc.fill(text, timeout=3000)
                step = MacroStep(action="fill", ref=cand.ref(), value_kind=kind,
                                 value="" if kind else text)
            elif decision.action == "dblclick":
                loc.dblclick(timeout=3000)
                step = MacroStep(action="dblclick", ref=cand.ref())
            else:
                loc.click(timeout=3000)
                step = MacroStep(action="click", ref=cand.ref())
            _settle(page)
            return True, step
        except Exception as e:  # noqa: BLE001 - a failed action is signal, not a crash
            self.ctx.history.append(f"action failed: {type(e).__name__}")
            return False, None

    def _value(self, decision: Decision) -> tuple[str, str]:
        """(text to type, kind to store). Secrets are only ever stored as their kind."""
        users = self.ctx.cfg.users
        if decision.value_kind:
            text = values.resolve(decision.value_kind, users)
            if text is not None:
                return text, decision.value_kind
        if decision.value:
            return decision.value, values.classify_literal(decision.value, users) or ""
        return values.VALUE_KINDS["valid_text"], "valid_text"

    def _use_macro(self, decision: Decision, offered: list[Macro]) -> None:
        ctx = self.ctx
        idx = decision.target_index
        if idx is None or not 0 <= idx < len(offered):
            ctx.history.append("macro failed: no such shortcut")
            return
        m = offered[idx]
        res = replay(m, ctx.session.page, ctx.session.base_url, ctx.cfg.users)
        if res.ok:
            ctx.stats["macro_replays"] += 1
            ctx.stats["macro_steps_saved"] += len(m.steps)
            ctx.macros.mark_replayed(m.key)
            ctx.history.append(f"macro ok: now on {m.target_path}")
            self._last_selector = m.target_path
            if self.recorder is not None:
                self.recorder.adopt(m)
        else:
            ctx.stats["macro_failures"] += 1
            ctx.macros.mark_failed(m.key, res.reason)
            offered.remove(m)
            ctx.history.append(f"macro failed ({res.reason}); continue from here by hand")
            self.recorder = None   # the prefix to this state is no longer known

    def _flush_macros(self) -> None:
        if self.recorder is None or self.ctx.macros is None:
            return
        import json
        for m in self.recorder.macros:
            from dataclasses import asdict
            raw = json.dumps(asdict(m), ensure_ascii=False)
            if any(secret and json.dumps(secret, ensure_ascii=False)[1:-1] in raw for user in self.ctx.cfg.users
                   for secret in (user.email, user.password)):
                continue
            self.ctx.macros.observe(m)
            self.ctx.stats["macros_recorded"] += 1

    # ------------------------------------------------------------------ helpers

    def _redact(self, text: str) -> str:
        for i, user in enumerate(self.ctx.cfg.users, 1):
            for credential_field in ("email", "password"):
                secret = getattr(user, credential_field, "")
                if secret:
                    text = text.replace(secret, f"<user{i}_{credential_field}>")
        return text

    def _describe(self, decision: Decision, candidates: list[Candidate]) -> str:
        idx = decision.target_index
        if decision.action in ("click", "dblclick", "fill") and idx is not None \
                and 0 <= idx < len(candidates):
            return candidates[idx].label()
        if decision.action == "macro":
            return f"M{decision.target_index}"
        return decision.target

    def target_for_step(self, decision: Decision) -> str:
        """A selector/path describing what the last action touched (for findings)."""
        return self._last_selector or decision.target

    def _path(self) -> str:
        return urlsplit(self.ctx.session.page.url).path or "/"

    def evaluate(self, decision) -> None:
        """Hook for subclasses to turn observations into findings. Base does nothing."""
        return


def _as_path(target: str) -> str:
    if not target:
        return "/"
    if target.startswith("http"):
        parts = urlsplit(target)
        return parts.path + (f"?{parts.query}" if parts.query else "")
    return target if target.startswith("/") else "/" + target


def _settle(page) -> None:
    try:
        page.wait_for_load_state("domcontentloaded", timeout=3000)
    except Exception:  # noqa: BLE001, S110 - optional load wait
        pass
