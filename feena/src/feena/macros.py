"""Memoized navigation: stop paying a model to rediscover the same path every run.

"Go to the forgot-password page" or "log in as alice and open my tasks" takes the same clicks on
run 1 and run 1000. The regular agent records its successful steps; every time it reaches a page
it hasn't been on this run, the steps that got it there become a *candidate macro* keyed by
``reach:<path>@<identity>``. Candidates that are observed again with the same steps gain
confidence, and ``feena macros promote`` (run it nightly) turns well-observed candidates into
*promoted* macros. Promoted macros are offered to agents as one-step shortcuts and replayed
deterministically with no model calls.

Guard rails, because a stale shortcut is worse than no shortcut:

* **What is memoized:** only the path *to* a state, recorded by the ``regular`` agent. The
  clumsy agent may *use* macros to get somewhere, but its chaotic actions are never recorded:
  the variation is the point of that agent.
* **Invalidation by blast radius:** macros that pass through a route the current diff affects
  are not offered this run, so changed pages are always explored fresh (and re-recorded).
* **Invalidation by replay:** each step must still resolve to exactly the element it recorded
  (role + accessible name), and the final page must have the recorded structure fingerprint.
  Any mismatch marks the macro ``stale``; the agent carries on from wherever it got to.
* **No secrets on disk:** credentials are stored as ``userN_password`` references and resolved
  from ``feena.yaml`` at replay time.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from . import values
from .actions import fingerprint, resolve
from .browser import same_origin, scoped_url

CANDIDATE, PROMOTED, STALE = "candidate", "promoted", "stale"
REPLAYABLE = ("click", "dblclick", "fill", "press", "goto", "back")


def safe_path(path: str) -> bool:
    parts = urlsplit(path)
    return (path.startswith("/") and not path.startswith("//") and not parts.scheme
            and not parts.netloc and not parts.query and not parts.fragment)


def safe_macro(macro: Macro) -> bool:
    return bool(macro.end_fingerprint) and all(safe_path(p) for p in [macro.target_path, *macro.paths]) and all(
        step.action in REPLAYABLE and (step.action != "goto" or safe_path(step.path))
        and (step.action != "fill" or bool(step.value_kind) and not step.value
             and (step.value_kind in values.VALUE_KINDS or values.is_secret(step.value_kind)))
        for step in macro.steps)


@dataclass
class MacroStep:
    action: str
    ref: dict | None = None        # {"role", "name", "nth"} for element actions
    path: str = ""                 # goto
    key: str = ""                  # press
    value_kind: str = ""           # fill: a values kind (secrets are only ever stored this way)
    value: str = ""                # fill: literal non-secret text, when no kind applies

    def signature(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    def describe(self) -> str:
        if self.ref:
            what = f'{self.ref["role"]} "{self.ref["name"]}"'
            if self.action == "fill":
                return f"fill {what} with <{self.value_kind or 'text'}>"
            return f"{self.action} {what}"
        return f"{self.action} {self.path or self.key}".strip()


@dataclass
class Macro:
    key: str
    target_path: str
    identity: str
    steps: list[MacroStep]
    end_fingerprint: str
    paths: list[str] = field(default_factory=list)   # every path the macro passes through
    status: str = CANDIDATE
    successes: int = 1
    failures: int = 0
    source: str = ""
    created: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    last_failure: str = ""

    @property
    def id(self) -> str:
        sig = "\n".join(s.signature() for s in self.steps)
        return hashlib.sha256(f"{self.key}\n{sig}".encode()).hexdigest()[:10]

    def describe(self) -> str:
        who = "anonymous" if self.identity == "anon" else f"logged in as {self.identity}"
        return f"reach {self.target_path} ({who}, {len(self.steps)} steps)"

    @classmethod
    def from_dict(cls, d: dict) -> Macro:
        d = dict(d)
        d["steps"] = [MacroStep(**s) for s in d.get("steps", [])]
        return cls(**d)


def macro_key(path: str, identity: str) -> str:
    return f"reach:{path}@{identity}"


# --------------------------------------------------------------------------- recording


class TraceRecorder:
    """Watches one agent run and cuts it into "how to reach page X" segments."""

    def __init__(self, start_path: str, source: str = ""):
        self.steps: list[MacroStep] = [MacroStep(action="goto", path=start_path)]
        self.landed: list[str] = [start_path]   # path the page was on after each step
        self.identity = "anon"
        self.source = source
        self.reached: set[str] = {start_path}
        self.macros: list[Macro] = []
        self._inherited: set[str] = set()   # paths walked by an adopted macro
        self._adopted_len = 0
        self._unsafe = not safe_path(start_path)

    def record(self, step: MacroStep, ok: bool, path_after: str, fp_after: str) -> None:
        if not ok or step.action not in REPLAYABLE:
            return  # failed actions changed nothing we can rely on
        if (not safe_path(path_after) or (step.action == "goto" and not safe_path(step.path))
                or (step.action == "fill" and (not step.value_kind or step.value))):
            self._unsafe = True
        if self._unsafe:
            return
        self.steps.append(step)
        self.landed.append(path_after)
        if step.value_kind.endswith("_password") and values.is_secret(step.value_kind):
            self.identity = step.value_kind.split("_", 1)[0]
        if path_after in self.reached:
            return
        self.reached.add(path_after)
        if not fp_after:
            return
        first = 0
        if self.identity == "anon":
            # Anonymous state lives in the URL: start from the last explicit navigation.
            first = max(i for i, s in enumerate(self.steps) if s.action == "goto")
        if len(self.steps) - first == 1:
            return  # "goto X" is already one action; nothing to memoize
        self.macros.append(Macro(
            key=macro_key(path_after, self.identity), target_path=path_after,
            identity=self.identity, steps=list(self.steps[first:]), end_fingerprint=fp_after,
            paths=sorted(set(self.landed[first:])
                         | (self._inherited if first < self._adopted_len else set())),
            source=self.source))

    def adopt(self, macro: Macro) -> None:
        """Continue recording on top of a successfully replayed macro."""
        self.steps = list(macro.steps)
        self.landed = [macro.target_path] * len(self.steps)
        self.identity = macro.identity
        self.reached.add(macro.target_path)
        self._inherited = set(macro.paths)
        self._adopted_len = len(self.steps)


# --------------------------------------------------------------------------- storage


class MacroStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.macros: dict[str, Macro] = {}
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text())
                for d in data.get("macros", []):
                    m = Macro.from_dict(d)
                    if safe_macro(m):
                        self.macros[m.key] = m
            except (ValueError, TypeError, KeyError):
                self.macros = {}  # a corrupt cache is a cold cache, never a crash

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "macros": [
            asdict(m) for m in sorted(self.macros.values(), key=lambda m: m.key)]}
        self.path.write_text(json.dumps(payload, indent=2) + "\n")

    def observe(self, new: Macro) -> str:
        """Fold a freshly recorded macro in. Returns what happened (for logs/tests)."""
        if not safe_macro(new):
            return "unsafe"
        old = self.macros.get(new.key)
        if old is None:
            self.macros[new.key] = new
            return "added"
        if old.id == new.id:
            old.successes += 1
            old.last_seen = time.time()
            old.end_fingerprint = new.end_fingerprint
            if old.status == STALE:
                old.status, old.failures, old.successes = CANDIDATE, 0, 1
            return "confirmed"
        if old.status == PROMOTED:
            return "kept-promoted"
        if old.status == STALE or len(new.steps) < len(old.steps):
            self.macros[new.key] = new
            return "replaced"
        return "ignored"

    def mark_failed(self, key: str, reason: str) -> None:
        m = self.macros.get(key)
        if m:
            m.failures += 1
            m.status = STALE
            m.last_failure = reason

    def mark_replayed(self, key: str) -> None:
        m = self.macros.get(key)
        if m:
            m.successes += 1
            m.last_seen = time.time()

    def promote(self, min_successes: int = 3) -> list[Macro]:
        done = []
        for m in self.macros.values():
            if m.status == CANDIDATE and m.successes >= min_successes and m.failures == 0:
                m.status = PROMOTED
                done.append(m)
        return done

    def prune(self, max_age_days: float = 30) -> list[str]:
        cutoff = time.time() - max_age_days * 86400
        gone = [k for k, m in self.macros.items() if m.status == STALE or m.last_seen < cutoff]
        for k in gone:
            del self.macros[k]
        return gone

    def offerable(self, identity: str | None = None, avoid_routes: list[str] = ()) -> list[Macro]:
        """Promoted macros safe to offer this run (none through diff-affected routes)."""
        from .blast_radius import route_matches

        out = []
        for m in sorted(self.macros.values(), key=lambda m: m.key):
            if m.status != PROMOTED or not safe_macro(m):
                continue
            if identity is not None and m.identity not in (identity, "anon"):
                continue
            if any(route_matches(r, p) for r in avoid_routes for p in m.paths):
                continue
            out.append(m)
        return out


# --------------------------------------------------------------------------- replay


@dataclass
class ReplayResult:
    ok: bool
    steps_done: int
    reason: str = ""


def replay(macro: Macro, page, base_url: str, users, timeout_ms: int = 3000) -> ReplayResult:
    if not safe_macro(macro):
        return ReplayResult(False, 0, "unsafe macro")
    base = base_url.rstrip("/")
    for i, step in enumerate(macro.steps):
        try:
            if step.action == "goto":
                page.goto(scoped_url(base, step.path))
            elif step.action == "back":
                page.go_back()
            elif step.action == "press":
                page.keyboard.press(step.key)
            else:
                loc = resolve(page, step.ref or {})
                if loc.count() == 0:
                    return ReplayResult(False, i, f"step {i + 1}: {step.describe()} not found")
                if step.action == "fill":
                    text = values.resolve(step.value_kind, users) if step.value_kind else step.value
                    if text is None:
                        return ReplayResult(False, i, f"step {i + 1}: no value for "
                                                      f"{step.value_kind}")
                    loc.fill(text, timeout=timeout_ms)
                elif step.action == "dblclick":
                    loc.dblclick(timeout=timeout_ms)
                else:
                    loc.click(timeout=timeout_ms)
            _settle(page)
            if not same_origin(base, page.url):
                return ReplayResult(False, i + 1, "navigation left target origin")
        except Exception as e:  # noqa: BLE001
            return ReplayResult(False, i, f"step {i + 1}: {step.describe()} failed: "
                                          f"{type(e).__name__}")
    path = urlsplit(page.url).path
    if path != macro.target_path:
        return ReplayResult(False, len(macro.steps), f"ended on {path}, not {macro.target_path}")
    try:
        fp = fingerprint(page)
    except Exception:  # noqa: BLE001 - fail closed without page structure
        return ReplayResult(False, len(macro.steps), "page structure unavailable")
    if fp != macro.end_fingerprint:
        return ReplayResult(False, len(macro.steps),
                            f"{macro.target_path} has a different structure than when recorded")
    return ReplayResult(True, len(macro.steps))


def _settle(page) -> None:
    try:
        page.wait_for_load_state("domcontentloaded", timeout=3000)
    except Exception:  # noqa: BLE001, S110 - optional load wait
        pass


# --------------------------------------------------------------------------- export


def export_pytest(macro: Macro) -> str:
    """A standalone Playwright pytest that walks the macro and checks it lands where expected.

    Same conventions as Feena's generated regression tests: base URL from ``FEENA_BASE_URL``,
    credentials from ``FEENA_USER<N>_EMAIL`` / ``_PASSWORD``, never written into the file.
    """
    if not safe_macro(macro):
        raise ValueError("Unsafe macro cannot be exported")
    lines = [
        f'"""Feena macro {macro.id}: {macro.describe()}. Generated; review before adopting."""',
        "import os",
        "",
        "from playwright.sync_api import sync_playwright",
        "",
        'BASE = os.environ.get("FEENA_BASE_URL", "http://localhost:3000").rstrip("/")',
        "",
        "",
        "def _secret(kind):",
        '    user, field = kind.split("_", 1)',
        '    return os.environ[f"FEENA_{user.upper()}_{field.upper()}"]',
        "",
        "",
        f"def test_reach_{_slug(macro.target_path)}_{macro.identity}():",
        "    with sync_playwright() as pw:",
        "        browser = pw.chromium.launch()",
        "        try:",
        "            _walk(browser.new_page())",
        "        finally:",
        "            browser.close()",
        "",
        "",
        "def _walk(page):",
    ]
    for s in macro.steps:
        if s.action == "goto":
            lines.append(f"    page.goto(BASE + {s.path!r})")
        elif s.action == "back":
            lines.append("    page.go_back()")
        elif s.action == "press":
            lines.append(f"    page.keyboard.press({s.key!r})")
        else:
            ref = s.ref or {}
            loc = (f"page.get_by_role({ref.get('role')!r}, name={ref.get('name')!r}, "
                   f"exact=True).nth({int(ref.get('nth', 0))})")
            if ref.get("password"):
                loc = (f"page.get_by_label({ref.get('name')!r}, exact=True)"
                       f".and_(page.locator('input[type=\"password\"]')).nth({int(ref.get('nth', 0))})")
            if s.action == "fill":
                if values.is_secret(s.value_kind):
                    val = f"_secret({s.value_kind!r})"
                elif s.value_kind:
                    val = repr(values.VALUE_KINDS.get(s.value_kind, ""))
                else:
                    val = repr(s.value)
                lines.append(f"    {loc}.fill({val})")
            else:
                lines.append(f"    {loc}.{s.action}()")
    lines.append(f"    assert page.url.split('?')[0].endswith({macro.target_path!r})")
    return "\n".join(lines) + "\n"


def _slug(path: str) -> str:
    s = "".join(c if c.isalnum() else "_" for c in path).strip("_")
    return s or "root"
