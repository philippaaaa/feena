"""Candidate menus, memoized macros, and tiered deciders, against a real browser."""
from __future__ import annotations

import json
import threading

import pytest
from flask import Flask, redirect, request
from flask import session as fsession
from werkzeug.serving import make_server

from feena.actions import candidates_from_tree, extract_candidates
from feena.agents.base import AgentContext
from feena.agents.clumsy import ClumsyAgent
from feena.agents.regular import RegularAgent
from feena.browser import session
from feena.config import Config, RunConfig, TargetConfig, UserConfig
from feena.llm import Decision, TieredDecider, parse
from feena.macros import PROMOTED, STALE, MacroStore, export_pytest

STATE = {"variant": "a"}


def _app() -> Flask:
    app = Flask(__name__)
    app.secret_key = "test"

    @app.get("/")
    def home():
        return ('<h1>Home</h1><a href="/login">log in</a> · <a href="/forgot">forgot password</a>')

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            if (request.form.get("email"), request.form.get("password")) == (
                    "alice@example.com", "s3cret-pw"):
                fsession["u"] = 1
                return '<script>location.href="/tasks"</script>'
            return "<p>bad login</p>", 401
        return ('<h1>Log in</h1><form method=post>'
                '<input name=email aria-label=email><input name=password type=password '
                'aria-label=password><button>log in</button></form>')

    @app.get("/forgot")
    def forgot():
        return '<h1>Forgot password</h1><input aria-label="email"><button>send link</button>'

    @app.get("/tasks")
    def tasks():
        if not fsession.get("u"):
            return redirect("/login")
        btn = "add" if STATE["variant"] == "a" else "create task"
        return f'<h1>My tasks</h1><input aria-label="new task"><button>{btn}</button>'

    return app


@pytest.fixture(scope="module")
def server():
    srv = make_server("127.0.0.1", 0, _app(), threaded=True)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()
    t.join(timeout=3)


@pytest.fixture(autouse=True)
def _reset():
    STATE["variant"] = "a"


def _cfg(tmp_path) -> Config:
    cfg = Config(target=TargetConfig(compose="", service="", port=0),
                 users=[UserConfig(label="alice", email="alice@example.com",
                                   password="s3cret-pw")],
                 run=RunConfig(max_steps=12, budget_seconds=60))
    cfg.root = tmp_path
    return cfg


class Scripted:
    """A deterministic decider: each entry picks an action by element *name*."""
    name = "scripted"
    available = True

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0
        self.requests = []

    def decide(self, req):
        self.calls += 1
        self.requests.append(req)
        if not self.script:
            return Decision(action="done")
        action, arg, extra = (self.script.pop(0) + ("", ""))[:3]
        if action in ("click", "fill", "dblclick"):
            idx = next(i for i, c in enumerate(req.candidates) if f'"{arg}"' in c)
            d = Decision(action=action, target=str(idx), reason="scripted")
            if action == "fill":
                if extra.startswith("literal:"):
                    d.value = extra.removeprefix("literal:")
                else:
                    d.value_kind = extra
            return d
        if action == "macro":
            idx = next(i for i, m in enumerate(req.macros) if arg in m)
            return Decision(action="macro", target=f"M{idx}")
        return Decision(action=action, target=arg)


LOGIN = [("click", "log in"), ("fill", "email", "user1_email"),
         ("fill", "password", "user1_password"), ("click", "log in"), ("done", "")]


def _run(agent_cls, server, tmp_path, decider, store, avoid=(), sub="run"):
    with session(server, tmp_path / sub) as sess:
        ctx = AgentContext(cfg=_cfg(tmp_path), session=sess, llm=decider, macros=store,
                           avoid_routes=list(avoid))
        agent_cls(ctx).run()
        return ctx, sess.page.url


# ------------------------------------------------------------------ candidates


def test_candidates_number_and_disambiguate():
    tree = {"role": "WebArea", "children": [
        {"role": "heading", "name": "Hi"},
        {"role": "button", "name": "Save"}, {"role": "button", "name": "Save"},
        {"role": "textbox", "name": "Email  "}, {"role": "button", "name": ""},
        {"role": "button", "name": "Off", "disabled": True}]}
    cs = candidates_from_tree(tree)
    assert [c.label() for c in cs] == [
        '[0] button "Save"', '[1] button "Save" (#2)', '[2] textbox "Email  "']
    assert cs[1].selector().endswith(">> nth=1") and cs[2].fillable


def test_candidates_resolve_on_real_page(tmp_path, server):
    with session(server, tmp_path / "c") as sess:
        sess.goto("/login")
        cs = extract_candidates(sess.page)
        assert {c.name for c in cs} >= {"email", "password", "log in"}
        for c in cs:
            assert c.locator(sess.page).count() == 1
            assert sess.page.locator(c.selector()).count() == 1


# ------------------------------------------------------------------ memoization


def test_regular_agent_records_then_replays_without_model(tmp_path, server):
    store = MacroStore(tmp_path / "macros.json")
    _run(RegularAgent, server, tmp_path, Scripted(LOGIN), store, sub="r1")
    assert {"reach:/login@anon", "reach:/tasks@user1"} <= set(store.macros)
    store.save()
    raw = (tmp_path / "macros.json").read_text()
    assert "s3cret-pw" not in raw and "alice@example.com" not in raw   # refs only
    assert "user1_password" in raw

    # Seen again with identical steps -> confirmed, then promoted.
    _run(RegularAgent, server, tmp_path, Scripted(LOGIN), store, sub="r2")
    assert store.macros["reach:/tasks@user1"].successes == 2
    assert [m.key for m in store.promote(min_successes=2)] == sorted(
        k for k in store.macros if store.macros[k].successes >= 2)

    # Next run: one decision to use the shortcut, one to stop.
    decider = Scripted([("macro", "reach /tasks"), ("done", "")])
    ctx, url = _run(RegularAgent, server, tmp_path, decider, store, sub="r3")
    assert decider.calls == 2
    assert url.endswith("/tasks")
    assert ctx.stats["macro_replays"] == 1 and ctx.stats["macro_steps_saved"] >= 4
    assert "logged in as user1" in decider.requests[0].macros[-1]


def test_literal_secret_is_redacted_to_reference(tmp_path, server):
    script = [("click", "log in"), ("fill", "email", "literal:alice@example.com"),
              ("fill", "password", "literal:s3cret-pw"), ("click", "log in"), ("done", "")]
    store = MacroStore(tmp_path / "m.json")
    _run(RegularAgent, server, tmp_path, Scripted(script), store)
    m = store.macros["reach:/tasks@user1"]
    kinds = [s.value_kind for s in m.steps if s.action == "fill"]
    assert kinds == ["user1_email", "user1_password"]
    assert all(s.value == "" for s in m.steps)


def test_changed_page_marks_macro_stale_and_agent_continues(tmp_path, server):
    store = MacroStore(tmp_path / "m.json")
    _run(RegularAgent, server, tmp_path, Scripted(LOGIN), store, sub="r1")
    store.promote(min_successes=1)
    STATE["variant"] = "b"   # the "add" button was renamed: structure changed
    decider = Scripted([("macro", "reach /tasks"), ("click", "create task"), ("done", "")])
    ctx, _ = _run(RegularAgent, server, tmp_path, decider, store, sub="r2")
    m = store.macros["reach:/tasks@user1"]
    assert m.status == STALE and "different structure" in m.last_failure
    assert ctx.stats["macro_failures"] == 1
    assert decider.calls == 3          # it kept going after the miss
    # A fresh, clean recording replaces the stale one as a new candidate.
    _run(RegularAgent, server, tmp_path, Scripted(LOGIN), store, sub="r3")
    assert store.macros["reach:/tasks@user1"].status == "candidate"


def test_blast_radius_routes_are_not_offered(tmp_path, server):
    store = MacroStore(tmp_path / "m.json")
    _run(RegularAgent, server, tmp_path, Scripted(LOGIN), store)
    store.promote(min_successes=1)
    assert any(m.key == "reach:/tasks@user1" for m in store.offerable())
    offered = store.offerable(avoid_routes=["/tasks"])
    assert all("/tasks" not in m.paths for m in offered)
    decider = Scripted([("done", "")])
    _run(RegularAgent, server, tmp_path, decider, store, avoid=["/tasks"], sub="r2")
    assert not any("/tasks" in m for m in decider.requests[0].macros)


def test_clumsy_agent_never_records(tmp_path, server):
    store = MacroStore(tmp_path / "m.json")
    _run(ClumsyAgent, server, tmp_path, Scripted(LOGIN), store)
    assert store.macros == {}


def test_export_is_runnable_python_without_secrets(tmp_path, server):
    store = MacroStore(tmp_path / "m.json")
    _run(RegularAgent, server, tmp_path, Scripted(LOGIN), store)
    src = export_pytest(store.macros["reach:/tasks@user1"])
    compile(src, "t.py", "exec")
    assert "s3cret-pw" not in src and "_secret('user1_password')" in src


def test_store_survives_corrupt_file(tmp_path):
    p = tmp_path / "m.json"
    p.write_text("{not json")
    assert MacroStore(p).macros == {}


# ------------------------------------------------------------------ deciders


class Fixed:
    def __init__(self, d, name="fixed"):
        self.d, self.name, self.available, self.calls = d, name, True, 0

    def decide(self, req):
        self.calls += 1
        return self.d


def test_tiered_decider_escalates_on_low_confidence_and_findings():
    smart = Fixed(Decision(action="done"), "smart")
    sure = TieredDecider(Fixed(Decision(action="click", target="1", confidence=0.9)), smart)
    assert sure.decide(None).action == "click" and smart.calls == 0
    unsure = TieredDecider(Fixed(Decision(action="click", target="1", confidence=0.3)), smart)
    assert unsure.decide(None).action == "done" and smart.calls == 1
    finding = TieredDecider(Fixed(Decision(action="note_finding", confidence=0.99)), smart)
    finding.decide(None)
    assert smart.calls == 2 and finding.stats == {"fast": 0, "escalated": 1}


def test_parse_is_strict():
    d = parse('```json\n{"action":"fill","target":"[3]","value_kind":"empty","confidence":7}\n```')
    assert (d.action, d.target_index, d.value_kind, d.confidence) == ("fill", 3, "empty", 1.0)
    assert parse('{"action":"rm -rf"}').action == "done"
    assert parse("[1,2]").action == "done"


def test_exported_macro_test_passes_against_the_app(tmp_path, server, monkeypatch):
    store = MacroStore(tmp_path / "m.json")
    _run(RegularAgent, server, tmp_path, Scripted(LOGIN), store)
    ns: dict = {}
    exec(compile(export_pytest(store.macros["reach:/tasks@user1"]), "t.py", "exec"), ns)  # noqa: S102 - execute generated test in isolated namespace
    monkeypatch.setenv("FEENA_USER1_EMAIL", "alice@example.com")
    monkeypatch.setenv("FEENA_USER1_PASSWORD", "s3cret-pw")
    ns["BASE"] = server
    test = next(v for k, v in ns.items() if k.startswith("test_reach_"))
    test()


class FakeLocator:
    def __init__(self, page, role=None, name=None, nth=0):
        self.page, self.role, self.name, self.ordinal = page, role, name, nth
    def aria_snapshot(self):
        return self.page.snapshot
    def nth(self, ordinal):
        return FakeLocator(self.page, self.role, self.name, ordinal)
    def count(self):
        return 1
    def click(self, **kwargs):
        self.page.actions.append(("click", self.role, self.name, self.ordinal))
    def fill(self, value, **kwargs):
        self.page.actions.append(("fill", value))


class FakePage:
    def __init__(self, snapshot='- heading "Home"\n- button "Save"'):
        self.snapshot, self.url, self.actions = snapshot, "http://target.test/", []
    def locator(self, selector):
        return FakeLocator(self)
    def get_by_role(self, role, name, exact):
        assert exact
        return FakeLocator(self, role, name)
    def goto(self, url):
        self.url = url
        self.actions.append(("goto", url))
    def wait_for_load_state(self, *args, **kwargs):
        pass


def test_modern_aria_snapshot_preserves_full_names_and_ordinals():
    name = "A" * 100 + " final"
    page = FakePage(f'- button "{name}" [disabled]\n- button "{name}"\n- textbox "Email"')
    candidates = extract_candidates(page)
    assert candidates[0].name == name
    assert candidates[0].nth == 1
    assert name in candidates[0].selector()
    candidates[0].locator(page).click()
    assert page.actions == [("click", "button", name, 1)]


def test_unavailable_accessibility_cannot_validate_macro():
    from feena.actions import fingerprint
    from feena.macros import Macro, MacroStep, replay
    with pytest.raises(ValueError):
        fingerprint(FakePage(""))
    m = Macro("reach:/@anon", "/", "anon", [MacroStep("goto", path="/")], "old")
    result = replay(m, FakePage(""), "http://target.test", [])
    assert not result.ok and "unavailable" in result.reason


def test_replay_detects_stale_structure_with_modern_snapshot():
    from feena.actions import fingerprint
    from feena.macros import Macro, MacroStep, replay
    before = fingerprint(FakePage())
    m = Macro("reach:/@anon", "/", "anon", [MacroStep("goto", path="/")], before)
    assert replay(m, FakePage(), "http://target.test", []).ok
    assert not replay(m, FakePage('- heading "Home"\n- button "Delete"'), "http://target.test", []).ok


def test_recorder_never_memoizes_unknown_literal_or_query():
    from feena.macros import MacroStep, TraceRecorder
    rec = TraceRecorder("/")
    rec.record(MacroStep("fill", ref={"role": "textbox", "name": "Code"}, value="unknown-secret"), True, "/", "fp")
    rec.record(MacroStep("click", ref={"role": "button", "name": "Next"}), True, "/next", "fp")
    assert not rec.macros
    rec = TraceRecorder("/")
    rec.record(MacroStep("goto", path="/callback?token=secret"), True, "/callback", "fp")
    rec.record(MacroStep("click", ref={"role": "button", "name": "Next"}), True, "/next", "fp")
    assert not rec.macros


def test_replay_resolves_secret_reference_without_persisting_value():
    from feena.actions import fingerprint
    from feena.macros import Macro, MacroStep, replay
    page = FakePage()
    m = Macro("reach:/@user1", "/", "user1", [MacroStep("fill", ref={"role": "textbox", "name": "Password"}, value_kind="user1_password")], fingerprint(page))
    assert replay(m, page, "http://target.test", _cfg(None).users).ok
    assert page.actions == [("fill", "s3cret-pw")]
    assert "s3cret-pw" not in json.dumps(__import__('dataclasses').asdict(m))


def test_scope_rejects_external_and_protocol_relative_navigation():
    from feena.browser import scoped_url
    assert scoped_url("http://target.test", "/home") == "http://target.test/home"
    for path in ["https://evil.test/", "//evil.test/", "javascript:alert(1)"]:
        with pytest.raises(ValueError):
            scoped_url("http://target.test", path)


def test_agent_records_promotes_and_replays_with_fake_browser(tmp_path):
    from feena.actions import _ax_tree
    from feena.browser import _flatten_ax
    class JourneyPage(FakePage):
        def get_by_role(self, role, name, exact):
            page = self
            class JourneyLocator(FakeLocator):
                def nth(self, ordinal):
                    self.ordinal = ordinal
                    return self
                def click(self, **kwargs):
                    super().click(**kwargs)
                    page.url = "http://target.test/done"
                    page.snapshot = '- heading "Done"\n- button "Again"'
            return JourneyLocator(self, role, name)
        def inner_text(self, *args):
            return "fine"
    class FakeSession:
        base_url = "http://target.test"
        def __init__(self):
            self.page = JourneyPage()
        def goto(self, path):
            from feena.browser import scoped_url
            self.page.goto(scoped_url(self.base_url, path))
        def snapshot(self):
            return _flatten_ax(_ax_tree(self.page))
    store = MacroStore(tmp_path / "macros.json")
    for _ in range(2):
        ctx = AgentContext(_cfg(tmp_path), FakeSession(), Scripted([("click", "Save"), ("done", "")]), macros=store)
        RegularAgent(ctx).run()
    m = store.macros["reach:/done@anon"]
    assert m.successes == 2
    store.promote(2)
    store.save()
    loaded = MacroStore(store.path)
    ctx = AgentContext(_cfg(tmp_path), FakeSession(), Scripted([("macro", "reach /done"), ("done", "")]), macros=loaded)
    RegularAgent(ctx).run()
    assert ctx.stats["macro_replays"] == 1
    assert ctx.session.page.url.endswith("/done")
    assert ctx.stats["macro_steps_saved"] == 2


def test_session_navigation_guard_blocks_redirects_and_external_requests(tmp_path):
    from types import SimpleNamespace

    from feena.browser import Session
    class Route:
        def __init__(self, url, status):
            self.request = SimpleNamespace(url=url, is_navigation_request=lambda: True)
            self.status, self.events = status, []
        def abort(self):
            self.events.append("abort")
        def fetch(self, **kwargs):
            assert kwargs == {"max_redirects": 0}
            self.events.append("fetch")
            return SimpleNamespace(status=self.status)
        def fulfill(self, **kwargs):
            self.events.append("fulfill")
    sess = Session("http://target.test", tmp_path)
    external = Route("http://evil.test", 200)
    sess._guard_navigation(external)
    assert external.events == ["abort"]
    redirecting = Route("http://target.test/login", 302)
    sess._guard_navigation(redirecting)
    assert redirecting.events == ["fetch", "abort"]
    safe = Route("http://target.test", 200)
    sess._guard_navigation(safe)
    assert safe.events == ["fetch", "fulfill"]


def test_agent_redacts_secret_prompt_and_history(tmp_path):
    from types import SimpleNamespace

    from feena.agents.base import BaseAgent
    page = FakePage()
    session = SimpleNamespace(page=page, base_url="http://target.test", goto=lambda _: None,
                              snapshot=lambda: "email alice@example.com password s3cret-pw")
    decider = Scripted([("done", "")])
    ctx = AgentContext(_cfg(tmp_path), session, decider, history=["s3cret-pw"])
    BaseAgent(ctx).run()
    req = decider.requests[0]
    assert "s3cret-pw" not in req.snapshot and "alice@example.com" not in req.snapshot
    assert req.history == ["<user1_password>"]


def test_password_candidates_use_exact_labels_and_export_secret_refs():
    from feena.actions import fingerprint
    from feena.macros import Macro, MacroStep, replay
    class PasswordLocator(FakeLocator):
        def evaluate_all(self, script):
            assert "value" not in script
            return [{"name": "Password", "disabled": False, "visible": True}]
        def and_(self, other):
            assert other.name == "password-filter"
            return self
        def nth(self, ordinal):
            return self
    class PasswordPage(FakePage):
        def locator(self, selector):
            return PasswordLocator(self, name="password-filter")
        def get_by_label(self, name, exact):
            assert name == "Password" and exact
            return PasswordLocator(self, name=name)
    page = PasswordPage()
    candidate = next(c for c in extract_candidates(page) if c.name == "Password")
    assert candidate.ref()["password"]
    candidate.locator(page).fill("private")
    m = Macro("reach:/@user1", "/", "user1", [MacroStep("fill", ref=candidate.ref(), value_kind="user1_password")], fingerprint(page))
    assert replay(m, page, "http://target.test", _cfg(None).users).ok
    exported = export_pytest(m)
    compile(exported, "generated.py", "exec")
    assert "get_by_label('Password', exact=True)" in exported
    assert "_secret('user1_password')" in exported
    assert "s3cret-pw" not in exported


def test_store_discards_unsafe_loaded_macro(tmp_path):
    from dataclasses import asdict

    from feena.macros import Macro, MacroStep
    p = tmp_path / "unsafe.json"
    m = Macro("reach:/next@anon", "/next", "anon", [MacroStep("goto", path="/login?token=secret")], "fp", status=PROMOTED)
    p.write_text(json.dumps({"macros": [asdict(m)]}))
    assert MacroStore(p).macros == {}
