"""Diff -> import graph -> affected routes."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from feena.blast_radius import analyze, compute, goal_hint, route_for_file, route_matches


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=repo,
                   check=True, capture_output=True)


def _write(repo: Path, rel: str, text: str) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "app"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _write(r, "tsconfig.json", '{ // comment\n "compilerOptions": {"baseUrl": ".", '
                               '"paths": {"@/*": ["./src/*"]},}}')
    _write(r, "src/components/Button.tsx", "export const Button = () => null\n")
    _write(r, "src/components/Form.tsx", 'import { Button } from "./Button"\nexport const F=1\n')
    _write(r, "src/app/settings/page.tsx", 'import { F } from "@/components/Form"\n')
    _write(r, "src/app/tasks/[id]/page.tsx", 'import { Button } from "../../../components/Button"\n')
    _write(r, "src/app/(marketing)/about/page.tsx", "export default 1\n")
    _write(r, "src/app/admin/layout.tsx", 'import "./admin.css"\n')
    _write(r, "src/app/admin/admin.css", "a{}\n")
    _write(r, "src/app/admin/users/page.tsx", "export default 1\n")
    _write(r, "src/lib/unused.ts", "export const x = 1\n")
    _write(r, "server.py", 'from flask import Flask, render_template\napp = Flask(__name__)\n\n'
                           '@app.route("/login")\ndef login():\n    return "x"\n\n\n'
                           '@app.get("/t/<int:tid>")\ndef task(tid):\n'
                           '    return render_template("task.html")\n')
    _write(r, "templates/task.html", "<p>t</p>\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "init")
    _git(r, "checkout", "-qb", "feature")
    return r


def _routes(br):
    return {h.route: h for h in br.routes}


def test_component_change_reaches_routes_through_alias_and_relative_imports(repo):
    _write(repo, "src/components/Button.tsx", "export const Button = () => 'changed'\n")
    br = compute(repo, "main")
    hits = _routes(br)
    assert set(hits) == {"/settings", "/tasks/[id]"}
    assert hits["/tasks/[id]"].distance == 1
    assert hits["/settings"].via == ["src/components/Button.tsx", "src/components/Form.tsx",
                                     "src/app/settings/page.tsx"]
    assert not br.is_global and br.unmapped == []
    assert "/settings" in goal_hint(br) and "Button" not in goal_hint(br)   # URLs only


def test_layout_change_fans_out_to_nested_pages_only(repo):
    _write(repo, "src/app/admin/admin.css", "a{color:red}\n")
    assert set(_routes(compute(repo, "main"))) == {"/admin/users"}


def test_python_route_hit_only_when_its_function_changes(repo):
    text = (repo / "server.py").read_text().replace('return "x"', 'return "y"')
    _write(repo, "server.py", text)
    _write(repo, "templates/task.html", "<p>changed</p>\n")
    assert set(_routes(compute(repo, "main"))) == {"/login", "/t/[tid]"}


def test_unlinked_and_global_changes_are_reported_not_hidden(repo):
    _write(repo, "src/lib/unused.ts", "export const x = 2\n")
    _write(repo, "package.json", "{}\n")
    br = compute(repo, "main")
    assert br.unmapped == ["src/lib/unused.ts"]
    assert br.is_global and "package.json" in br.global_reasons
    assert goal_hint(br) == ""     # a global change shouldn't narrow the agents


def test_untracked_new_component_counts(repo):
    _write(repo, "src/components/New.tsx", "export const N = 1\n")
    _write(repo, "src/app/settings/page.tsx",
           'import { F } from "@/components/Form"\nimport { N } from "@/components/New"\n')
    assert "/settings" in _routes(compute(repo, "main"))


def test_analyze_without_git_lines_treats_whole_python_file_as_changed(repo):
    br = analyze(repo, "main", ["server.py"])
    assert set(_routes(br)) == {"/login", "/t/[tid]"}


def test_deleted_component_still_reaches_existing_importers(repo):
    (repo / "src/components/Button.tsx").unlink()
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "remove button")
    br = compute(repo, "main")
    assert set(_routes(br)) == {"/settings", "/tasks/[id]"}


def test_deleted_page_still_identifies_its_route(repo):
    (repo / "src/app/(marketing)/about/page.tsx").unlink()
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "remove about")
    assert "/about" in _routes(compute(repo, "main"))


def test_rename_reports_both_old_and_new_routes(repo):
    _git(repo, "mv", "src/app/(marketing)/about", "src/app/(marketing)/contact")
    br = compute(repo, "main")
    assert set(_routes(br)) == {"/about", "/contact"}


def test_missing_base_is_an_explicit_git_error(repo):
    with pytest.raises(subprocess.CalledProcessError):
        compute(repo, "missing-ref")


@pytest.mark.parametrize("base", ["--output=unsafe", "-h", "", "main\0bad"])
def test_base_cannot_be_interpreted_as_git_option(repo, base):
    with pytest.raises(ValueError):
        compute(repo, base)
    assert not (repo / "unsafe").exists()


def test_repository_with_spaces_and_dash_path_is_safe(tmp_path):
    r = tmp_path / "-test repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _write(r, "pages/about me.tsx", "export default 1\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "init")
    _write(r, "pages/about me.tsx", "export default 2\n")
    br = compute(r, "main")
    assert br.changed == ["pages/about me.tsx"]
    assert set(_routes(br)) == {"/about me"}


def test_blast_cli_reports_missing_ref_and_prints_json(repo):
    import json

    from click.testing import CliRunner

    from feena.cli import main

    runner = CliRunner()
    missing = runner.invoke(main, ["blast-radius", "--repo", str(repo), "--base", "missing-ref"])
    assert missing.exit_code == 1 and "git failed" in missing.output
    _write(repo, "src/components/Button.tsx", "export const Button = () => 3\n")
    result = runner.invoke(main, ["blast-radius", "--repo", str(repo), "--base", "main", "--json"])
    assert result.exit_code == 0, result.output
    assert {r["route"] for r in json.loads(result.output)["routes"]} == {"/settings", "/tasks/[id]"}


def test_macro_cli_shows_steps_and_resets_only_selected_macro(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from feena import cli
    from feena.macros import Macro, MacroStep, MacroStore

    store = MacroStore(tmp_path / "macros.json")
    first = Macro("reach:/one@anon", "/one", "anon", [MacroStep("goto", path="/one")], "one")
    second = Macro("reach:/two@anon", "/two", "anon", [MacroStep("goto", path="/two")], "two")
    store.observe(first)
    store.observe(second)
    store.save()
    monkeypatch.setattr(cli, "_store", lambda _: store)
    runner = CliRunner()
    shown = runner.invoke(cli.main, ["macros", "show", first.id])
    assert shown.exit_code == 0 and "1. goto /one" in shown.output
    no_selection = runner.invoke(cli.main, ["macros", "reset"])
    assert no_selection.exit_code == 2
    assert len(store.macros) == 2
    reset = runner.invoke(cli.main, ["macros", "reset", first.id])
    assert reset.exit_code == 0, reset.output
    assert set(MacroStore(store.path).macros) == {second.key}
    reset_all = runner.invoke(cli.main, ["macros", "reset", "--all"])
    assert reset_all.exit_code == 0
    assert not MacroStore(store.path).macros


@pytest.mark.parametrize("rel,expected", [
    ("app/page.tsx", ("page", "/")),
    ("src/app/(shop)/cart/@modal/page.tsx", ("page", "/cart")),
    ("pages/blog/[slug].tsx", ("page", "/blog/[slug]")),
    ("pages/index.jsx", ("page", "/")),
    ("pages/api/x.ts", None),
    ("pages/_app.tsx", ("layout", "/")),
    ("src/routes/docs/[...rest]/+page.svelte", ("page", "/docs/[...rest]")),
    ("app/routes/notes.$id.edit.tsx", ("page", "/notes/[id]/edit")),
    ("app/routes/_index.tsx", ("page", "/")),
    ("src/components/x.tsx", None),
])
def test_route_detection(rel, expected):
    assert route_for_file(rel) == expected


@pytest.mark.parametrize("pattern,path,ok", [
    ("/tasks/[id]", "/tasks/3", True),
    ("/tasks/[id]", "/tasks", False),
    ("/tasks/[id]", "/tasks/3/edit", False),
    ("/t/[tid]", "/t/9?x=1", True),
    ("/docs/[...slug]", "/docs/a/b", True),
    ("/docs/[...slug]", "/docs", False),
    ("/docs/[[...slug]]", "/docs", True),
    ("/", "/", True),
    ("/login", "/logout", False),
])
def test_route_matches(pattern, path, ok):
    assert route_matches(pattern, path) is ok
