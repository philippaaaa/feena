"""``feena`` command line entry point."""
from __future__ import annotations

import shutil
import sys
from contextlib import contextmanager
from pathlib import Path

import click
from rich.console import Console

from .agents.base import AgentContext
from .agents.clumsy import ClumsyAgent
from .agents.hostile import HostileAgent
from .agents.regular import RegularAgent
from .browser import session
from .config import Config, TargetConfig, load_config
from .findings import Finding, confirm, dedup
from .llm import make_decider
from .outcomes import render_outcomes, run_outcomes
from .reporter import render_markdown, write_report
from .sandbox import Sandbox, SandboxError, SandboxManager, attach, wait_healthy
from .verification import verify_finding

console = Console()

EXPLORATORY = {"regular": RegularAgent, "clumsy": ClumsyAgent}
FAIL_ON = ["critical", "high", "medium", "low", "none"]


@click.group()
def main() -> None:
    """Feena — agents that use, break, and attack your app in a sandbox."""


@main.command()
@click.option("--config", "config_path", default="feena.yaml")
@click.option("--url", required=True, help="Your disposable local/private preview app.")
@click.option("--scenario", default=None, help="Run one configured browser journey.")
@click.option("--wait", default=60, type=click.IntRange(min=1))
def simulate(config_path, url, scenario, wait):
    """Run browser journeys across real network conditions; no model or security scan required."""
    from .simulation import render_simulations, run_simulations

    cfg = load_config(config_path)
    selected = [s for s in cfg.scenarios if scenario is None or s.name == scenario]
    if not selected:
        raise click.ClickException("No matching scenarios configured; add scenarios to feena.yaml.")
    try:
        with _target(cfg, url, wait) as sandbox:
            results = run_simulations(selected, sandbox.base_url, cfg.out_path / "simulations")
    except SandboxError as exc:
        raise click.ClickException(str(exc)) from exc
    body = render_simulations(results)
    console.print(body, markup=False)
    cfg.out_path.mkdir(parents=True, exist_ok=True)
    (cfg.out_path / "simulation-report.md").write_text(body)
    if any(r.status != "passed" for r in results):
        sys.exit(1)


@main.command("replay-simulation")
@click.argument("manifest", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--url", required=True, help="Freshly reset local/private target; never inferred from evidence.")
@click.option("--out", default=".feena/simulations", type=click.Path(path_type=Path))
def replay_simulation(manifest, url, out):
    """Rerun a saved journey and fault profile. Reset/seed application data first."""
    import json

    from .simulation import render_simulations, run_simulations
    from .simulation_config import BrowserScenario, NetworkProfile

    try:
        data = json.loads(manifest.read_text())
        if data.get("version") != 1:
            raise ValueError("unsupported manifest version")
        scenario = BrowserScenario.model_validate(data["scenario"])
        profile = NetworkProfile.model_validate(data["profile"])
        scenario.profiles = [profile]
        sandbox = attach(url)
    except (ValueError, KeyError, TypeError, SandboxError) as exc:
        raise click.ClickException(f"Cannot replay manifest ({type(exc).__name__}).") from exc
    results = run_simulations([scenario], sandbox.base_url, out)
    console.print(render_simulations(results), markup=False)
    if any(r.status != "passed" for r in results):
        sys.exit(1)


def _load_or_default(config_path: str) -> Config:
    """feena.yaml if present; otherwise minimal defaults (enough to attach to a running app)."""
    try:
        return load_config(config_path)
    except FileNotFoundError:
        return Config(target=TargetConfig(compose="", service="", port=0))


@contextmanager
def _target(cfg: Config, url: str | None, wait: int):
    """Yield a Sandbox: attach to an already-running LOCAL app, or build one with Docker."""
    if url:
        sb = attach(url)  # refuses anything that is not loopback/private
        if not wait_healthy(sb, cfg.target.healthcheck, wait):
            raise SandboxError(f"App at {url} did not answer {cfg.target.healthcheck} "
                               f"within {wait}s. Is it running?")
        yield sb
    else:
        with SandboxManager(cfg) as sb:
            yield sb


def _scan(cfg: Config, sandbox: Sandbox, agents: list[str], blast=None):
    """Run agents, dedup, and keep only findings that reproduce. -> (confirmed, dropped_count)."""
    llm = make_decider(cfg.models)
    if not llm.available and any(a in EXPLORATORY for a in agents):
        console.print("[yellow]No configured model credentials: exploratory agents will no-op. "
                      "Hostile checks still run.[/yellow]")
    findings = dedup(_run_agents(cfg, sandbox, agents, llm, blast))
    confirmed, dropped = confirm(findings, _make_replayer(cfg, sandbox))
    import json
    cfg.out_path.mkdir(parents=True, exist_ok=True)
    (cfg.out_path / "verification.json").write_text(json.dumps([
        {"fingerprint": f.fingerprint, "kind": f.kind.value,
         "status": f.verification_status, "reason": f.verification_reason}
        for f in findings
    ], indent=2) + "\n")
    return confirmed, len(dropped)


def _blast(cfg: Config, base: str | None):
    """Compute the diff's blast radius if a base ref is configured; None otherwise."""
    import subprocess

    from .blast_radius import compute

    base = base or cfg.blast_radius.base
    if not base:
        return None
    try:
        br = compute(cfg.repo_path, base)
    except (subprocess.CalledProcessError, OSError, ValueError) as e:
        console.print(f"[yellow]Blast radius skipped (git diff against {base!r} failed: "
                      f"{type(e).__name__}). Running unfocused.[/yellow]")
        return None
    cfg.out_path.mkdir(parents=True, exist_ok=True)
    (cfg.out_path / "blast-radius.json").write_text(br.to_json() + "\n")
    scope = "global change" if br.is_global else f"{len(br.routes)} affected route(s)"
    console.print(f"Blast radius vs {base}: {len(br.changed)} changed file(s), {scope}")
    return br


def _extra_sections(cfg: Config, blast) -> str:
    import json

    parts = []
    if blast is not None:
        parts.append(blast.render_markdown())
    stats_file = cfg.out_path / "run-stats.json"
    if stats_file.exists():
        st = json.loads(stats_file.read_text())
        if st.get("decisions") or st.get("macro_replays"):
            parts.append(
                f"_Agent effort: {st['decisions']} model decision(s); "
                f"{st['macro_replays']} memoized shortcut(s) replayed "
                f"({st['macro_steps_saved']} step(s) without a model call); "
                f"{st['macro_failures']} shortcut(s) went stale._")
    return ("\n\n" + "\n\n".join(parts)) if parts else ""


@main.command()
@click.option("--config", "config_path", default="feena.yaml", help="Path to feena.yaml.")
@click.option("--agent", "only_agent", default=None, help="Run just one agent.")
@click.option("--headed", is_flag=True, help="Show the browser (exploratory agents).")
@click.option("--url", default=None, help="Attach to an app already running locally instead of building a sandbox.")
@click.option("--wait", default=60, show_default=True, help="Seconds to wait for --url to become healthy.")
@click.option("--base", default=None, help="Git ref to diff against; focuses agents on the "
                                           "routes the change affects (blast radius).")
def run(config_path: str, only_agent: str | None, headed: bool, url: str | None, wait: int,
        base: str | None) -> None:
    """Boot the app in a sandbox, run the agents, verify, and report."""
    cfg = _load_or_default(config_path) if url else load_config(config_path)
    if headed:
        cfg.run.headed = True
    agents = [only_agent] if only_agent else cfg.run.agents

    console.print(f"[bold]Feena[/bold] · agents: {', '.join(agents)}")
    blast = _blast(cfg, base)
    try:
        with _target(cfg, url, wait) as sandbox:
            console.print(f"Target: {sandbox.base_url}" + ("" if url else " (sandbox, no egress)"))
            confirmed, dropped = _scan(cfg, sandbox, agents, blast)
            outcomes = run_outcomes(cfg.outcomes, sandbox.base_url, cfg.users)
            simulations = _simulations(cfg, sandbox.base_url)
            _emit(cfg, confirmed, dropped, agents, outcomes, simulations,
                  extra=_extra_sections(cfg, blast))
    except SandboxError as e:
        console.print(f"[red]Sandbox error:[/red] {e}")
        sys.exit(2)


def _run_agents(cfg, sandbox: Sandbox, agents: list[str], llm, blast=None) -> list[Finding]:
    import json

    from .blast_radius import goal_hint
    from .macros import MacroStore

    out: list[Finding] = []
    out_root = cfg.out_path
    store = MacroStore(cfg.macros_path) if cfg.macros.enabled else None
    hint = goal_hint(blast) if blast is not None else ""
    # Global changes invalidate all known navigation shortcuts.
    avoid = (["/[[...path]]"] if blast.is_global else blast.affected_routes) if blast else []
    totals: dict[str, int] = {}
    (out_root / "run-stats.json").unlink(missing_ok=True)   # never report a previous run's numbers
    for name in agents:
        console.print(f"→ running [cyan]{name}[/cyan]")
        if name == "hostile":
            out.extend(HostileAgent(cfg, sandbox).run())
        elif name in EXPLORATORY:
            agent_dir = out_root / "runs" / name
            with session(sandbox.base_url, agent_dir, headed=cfg.run.headed) as sess:
                ctx = AgentContext(cfg=cfg, session=sess, llm=llm, macros=store,
                                   focus_hint=hint, avoid_routes=avoid)
                out.extend(EXPLORATORY[name](ctx).run())
                for k, v in ctx.stats.items():
                    totals[k] = totals.get(k, 0) + v
        else:
            console.print(f"[yellow]unknown agent '{name}', skipping[/yellow]")
    if store is not None and totals:
        store.save()
    if totals:
        tiered = getattr(llm, "stats", None)
        if tiered:
            totals.update({f"decider_{k}": v for k, v in tiered.items()})
        out_root.mkdir(parents=True, exist_ok=True)
        (out_root / "run-stats.json").write_text(json.dumps(totals, indent=2) + "\n")
    return out


def _make_replayer(cfg, sandbox: Sandbox):
    """Return a finding-specific replay result using its built-in reproduction spec."""
    return lambda finding: verify_finding(finding, cfg, sandbox)


def _simulations(cfg, base_url):
    if not cfg.scenarios:
        return []
    from .simulation import run_simulations
    return run_simulations(cfg.scenarios, base_url, cfg.out_path / "simulations")


def _emit(cfg, confirmed: list[Finding], dropped_count: int, agents: list[str], outcomes=(),
          simulations=(), extra: str = "") -> None:
    report_path = write_report(confirmed, dropped_count, cfg.out_path, cfg.outcomes)
    body = render_markdown(confirmed, dropped_count) + extra
    if outcomes:
        body += "\n\n" + render_outcomes(outcomes)
    if simulations:
        from .simulation import render_simulations
        body += "\n\n" + render_simulations(simulations)
    report_path.write_text(body)
    console.print(body, markup=False)
    console.print(f"\n[green]Report written:[/green] {report_path}")
    if cfg.report.format == "github":
        from .ci import upsert_pr_comment
        console.print(f"PR comment: {upsert_pr_comment(body)}")
    if cfg.attest.enabled:
        from .attest import write_attestation
        key = (cfg.root / cfg.attest.key).resolve()
        if key.exists():
            j, m = write_attestation(confirmed, agents, dropped_count, key, cfg.out_path)
            console.print(f"[green]Signed attestation:[/green] {j}  ·  {m}")
        else:
            console.print(f"[yellow]attest.enabled but no key at {key}; run `feena keygen`.[/yellow]")
    if cfg.corpus.enabled:
        from .corpus import record
        path = record(confirmed, cfg.corpus.stack, cfg.out_path)
        console.print(f"Corpus: anonymised records appended to {path} (not uploaded).")
    # Non-zero exit on any HIGH/CRITICAL so a PR check can gate on it.
    from .findings import Severity
    if (any(f.severity in (Severity.HIGH, Severity.CRITICAL) for f in confirmed)
            or any(o.status != "passed" for o in outcomes)
            or any(s.status != "passed" for s in simulations)):
        sys.exit(1)


@main.command()
@click.option("--targets", "targets_path", default="benchmark/targets.yaml", help="Target list.")
@click.option("--only", default=None, help="Run just one target by name.")
@click.option("--out", "out_dir", default="./.feena", help="Where to write the report.")
def bench(targets_path: str, only: str | None, out_dir: str) -> None:
    """Run the hostile agent across many sandboxed apps and aggregate the results."""
    from .bench import load_targets, render_bench_md, run_bench, write_bench
    targets = load_targets(targets_path)
    console.print(f"[bold]Feena benchmark[/bold] · {len(targets)} target(s)"
                  + (f" · only {only}" if only else ""))
    for t in targets:
        console.print(f"  • {t.name} [dim]({t.kind})[/dim]")
    results = run_bench(targets, only=only)
    path = write_bench(results, Path(out_dir))
    console.print(render_bench_md(results))
    console.print(f"\n[green]Benchmark written:[/green] {path}")


@main.command()
@click.option("--base-url", required=True, help="The app under test (a local/sandbox copy).")
@click.option("--config", "config_path", default="feena.yaml", help="For seeded test users.")
@click.option("--tests", "tests_dir", default=None, help="Generated tests dir (default .feena/tests).")
def regress(base_url: str, config_path: str, tests_dir: str | None) -> None:
    """Run the generated regression tests against an app. No API key, no Feena agents."""
    import os
    import subprocess
    env = os.environ.copy()
    env["FEENA_BASE_URL"] = base_url
    out_root = Path("./.feena")
    try:
        cfg = load_config(config_path)
        out_root = cfg.out_path
        for i, u in enumerate(cfg.users[:2], 1):
            env[f"FEENA_USER{i}_EMAIL"] = u.email
            env[f"FEENA_USER{i}_PASSWORD"] = u.password
    except FileNotFoundError:
        console.print("[yellow]No feena.yaml; tests that need a login will skip.[/yellow]")
    target = Path(tests_dir) if tests_dir else out_root / "tests"
    if not target.exists():
        console.print(f"[red]No tests at {target}. Run `feena run` first.[/red]")
        sys.exit(2)
    sys.exit(subprocess.call([sys.executable, "-m", "pytest", "-q", str(target)], env=env))


@main.command()
@click.option("--url", default=None, help="App already running locally (recommended in CI). "
                                          "Only loopback/private addresses are accepted.")
@click.option("--config", "config_path", default="feena.yaml", help="For seeded test users.")
@click.option("--tests", "tests_dir", default="tests/feena", show_default=True,
              help="Committed regression tests to run against the same app.")
@click.option("--baseline", "baseline_path", default="feena.baseline.json", show_default=True,
              help="Known/accepted findings; CI fails only on findings not listed here.")
@click.option("--fail-on", type=click.Choice(FAIL_ON), default="high", show_default=True,
              help="Fail on NEW findings at or above this severity ('none' = never on findings).")
@click.option("--wait", default=60, show_default=True, help="Seconds to wait for the app to be healthy.")
@click.option("--comment/--no-comment", default=True, help="Post/update the sticky PR comment.")
@click.option("--agent", "only_agent", default=None, help="Run just one agent.")
@click.option("--base", default=None, help="Git ref to diff against (e.g. origin/main) to focus "
                                           "agents on the routes this PR affects.")
def ci(url, config_path, tests_dir, baseline_path, fail_on, wait, comment, only_agent,
       base) -> None:
    """The PR check: scan, run committed regression tests, comment once, fail on NEW problems."""
    from . import ci as ci_mod

    cfg = _load_or_default(config_path)
    agents = [only_agent] if only_agent else [
        a for a in cfg.run.agents if a == "hostile" or make_decider(cfg.models).available]
    console.print(f"[bold]Feena CI[/bold] · agents: {', '.join(agents)}")
    blast = _blast(cfg, base)
    try:
        with _target(cfg, url, wait) as sandbox:
            confirmed, dropped = _scan(cfg, sandbox, agents, blast)
            report_path = write_report(confirmed, dropped, cfg.out_path, cfg.outcomes)
            outcomes = run_outcomes(cfg.outcomes, sandbox.base_url, cfg.users)
            simulations = _simulations(cfg, sandbox.base_url)
            baseline = ci_mod.load_baseline(Path(baseline_path))
            new, known, fixed = ci_mod.split_by_baseline(confirmed, baseline)
            regression = ci_mod.run_regression(Path(tests_dir), sandbox.base_url, cfg.users)
    except SandboxError as e:
        console.print(f"[red]Sandbox error:[/red] {e}")
        sys.exit(2)

    verdict = ci_mod.decide(new, regression, fail_on)
    unsuccessful = sum(o.status != "passed" for o in outcomes)
    if unsuccessful:
        verdict.failed = True
        verdict.reasons.append(f"{unsuccessful} user outcome(s) failed or inconclusive")
    if any(s.status != "passed" for s in simulations):
        verdict.failed = True
        verdict.reasons.append("browser simulations failed or were inconclusive")
    body = ci_mod.render_comment(new, known, fixed, regression, verdict, dropped,
                                 fail_on, has_baseline=bool(baseline))
    extra = _extra_sections(cfg, blast)
    if extra:
        body += extra
        report_path.write_text(report_path.read_text() + extra)
    if outcomes:
        summary = "\n\n" + render_outcomes(outcomes)
        body += summary
        report_path.write_text(report_path.read_text() + summary)
    if simulations:
        from .simulation import render_simulations
        summary = "\n\n" + render_simulations(simulations)
        body += summary
        report_path.write_text(report_path.read_text() + summary)
    (cfg.out_path / "comment.md").write_text(body)
    ci_mod.write_step_summary(body)
    if comment:
        console.print(f"PR comment: {ci_mod.upsert_pr_comment(body)}")
    ci_mod.write_outputs(len(new), regression.failed, verdict.failed)

    console.print(f"{len(confirmed)} confirmed · {len(new)} new · {len(known)} baselined · "
                  f"regression tests: " + (f"{regression.passed} passed, {regression.failed} failed, "
                                           f"{regression.skipped} skipped" if regression.ran
                                           else regression.note))
    console.print(f"Full report: {report_path}")
    if verdict.failed:
        console.print("[red]FAIL:[/red] " + "; ".join(verdict.reasons), markup=True)
        sys.exit(1)
    console.print("[green]PASS[/green]")


@main.command()
@click.option("--url", default=None, help="App already running locally.")
@click.option("--config", "config_path", default="feena.yaml")
@click.option("--baseline", "baseline_path", default="feena.baseline.json", show_default=True)
@click.option("--wait", default=60, show_default=True)
def baseline(url, config_path, baseline_path, wait) -> None:
    """Accept the current findings so CI fails only on NEW ones. Commit the file."""
    from . import ci as ci_mod

    cfg = _load_or_default(config_path)
    try:
        with _target(cfg, url, wait) as sandbox:
            confirmed, _ = _scan(cfg, sandbox, ["hostile"])
    except SandboxError as e:
        console.print(f"[red]Sandbox error:[/red] {e}")
        sys.exit(2)
    n = ci_mod.write_baseline(confirmed, Path(baseline_path))
    console.print(f"Baselined {n} finding(s) -> {baseline_path}. Commit it. "
                  "Re-run this after fixes to prune fixed entries.")


@main.command()
@click.argument("fingerprints", nargs=-1)
@click.option("--all", "adopt_all", is_flag=True, help="Adopt every generated test.")
@click.option("--from", "src", default=".feena/tests", show_default=True)
@click.option("--to", "dest", default="tests/feena", show_default=True)
def adopt(fingerprints, adopt_all, src, dest) -> None:
    """Copy generated regression tests into your repo. Do this AFTER fixing the bug:
    a committed test for a bug that still exists fails CI by design."""
    srcp, destp = Path(src), Path(dest)
    available = {p.stem.removeprefix("test_"): p for p in srcp.glob("test_*.py")}
    if not available:
        console.print(f"[red]No generated tests in {srcp}. Run `feena run` first.[/red]")
        sys.exit(2)
    wanted = list(available) if adopt_all else list(fingerprints)
    if not wanted:
        console.print("Nothing selected. Pass fingerprints (see the report) or --all.")
        sys.exit(2)
    missing = [w for w in wanted if w not in available]
    if missing:
        console.print(f"[red]Unknown fingerprint(s): {', '.join(missing)}[/red]")
        sys.exit(2)
    destp.mkdir(parents=True, exist_ok=True)
    for support in ("_feena_support.py", "conftest.py"):
        shutil.copy(srcp / support, destp / support)
    if (srcp / "_feena_outcomes.py").exists():
        shutil.copy(srcp / "_feena_outcomes.py", destp / "_feena_outcomes.py")
    for w in wanted:
        shutil.copy(available[w], destp / available[w].name)
    console.print(f"Adopted {len(wanted)} test(s) into {destp}. Commit them. Each passes while "
                  "its bug stays fixed and fails on the PR that brings it back.")


@main.command()
@click.option("--out", default=".", help="Directory to write the keypair into.")
def keygen(out: str) -> None:
    """Create an Ed25519 keypair for signing attestations."""
    from .attest import generate_keypair
    priv, pub = generate_keypair(Path(out))
    console.print(f"Private key (keep secret, add to CI secrets): {priv}")
    console.print(f"Public key (share with auditors): {pub}")


@main.command()
@click.argument("attestation", type=click.Path(exists=True))
@click.option("--pub", required=True, type=click.Path(exists=True), help="Public key PEM.")
def verify(attestation: str, pub: str) -> None:
    """Verify a signed attestation has not been altered."""
    import json

    from .attest import verify as _verify
    att = json.loads(Path(attestation).read_text())
    if _verify(att, Path(pub)):
        r = att["record"]
        console.print(f"[green]VALID[/green] · commit {r.get('commit') or 'unknown'} · tested {r['tested_at']} "
                      f"· {r['summary']['confirmed']} confirmed finding(s)")
    else:
        console.print("[red]INVALID[/red] · the record was altered or signed with a different key")
        sys.exit(1)


@main.group()
def corpus() -> None:
    """Inspect or upload the local anonymised findings corpus."""


@corpus.command("stats")
@click.option("--config", "config_path", default="feena.yaml")
def corpus_stats(config_path: str) -> None:
    """Most common verified bug patterns in the local corpus."""
    from .corpus import summarize
    cfg = load_config(config_path)
    rows = summarize(cfg.out_path / "corpus.jsonl")
    if not rows:
        console.print("Corpus is empty. Enable `corpus.enabled` and run Feena.")
    for check, route, n in rows:
        console.print(f"{n:>4}  {check}  [dim]{route}[/dim]")


@corpus.command("upload")
@click.option("--config", "config_path", default="feena.yaml")
def corpus_upload(config_path: str) -> None:
    """Explicitly send anonymised records to the configured endpoint."""
    import os

    from .corpus import upload
    cfg = load_config(config_path)
    if not cfg.corpus.endpoint:
        console.print("[yellow]No corpus.endpoint configured; nothing sent.[/yellow]")
        return
    n = upload(cfg.out_path / "corpus.jsonl", cfg.corpus.endpoint, os.environ.get("FEENA_TOKEN"))
    console.print(f"Uploaded {n} anonymised record(s).")


@main.command("blast-radius")
@click.option("--base", default="main", show_default=True, help="Git ref to diff against.")
@click.option("--repo", default=".", show_default=True, help="Repository root.")
@click.option("--json", "as_json", is_flag=True, help="Print JSON instead of Markdown.")
def blast_radius_cmd(base: str, repo: str, as_json: bool) -> None:
    """Show which routes a diff affects, and the import chain that links them."""
    import subprocess

    from .blast_radius import compute
    try:
        br = compute(repo, base)
    except subprocess.CalledProcessError as e:
        raise click.ClickException(f"git failed: {(e.stderr or '').strip() or e}") from e
    except (OSError, ValueError) as e:
        raise click.ClickException(str(e)) from e
    if as_json:
        # Rich wraps long paths at terminal width, which can make JSON invalid.
        click.echo(br.to_json())
    else:
        console.print(br.render_markdown(), markup=False)


@main.group()
def macros() -> None:
    """Memoized navigation shortcuts recorded by the regular agent."""


def _store(config_path: str):
    from .macros import MacroStore
    return MacroStore(_load_or_default(config_path).macros_path)


@macros.command("list")
@click.option("--config", "config_path", default="feena.yaml")
def macros_list(config_path: str) -> None:
    """Every recorded shortcut and its status."""
    store = _store(config_path)
    if not store.macros:
        console.print("No macros yet. They are recorded by `feena run` (regular agent).")
    for m in sorted(store.macros.values(), key=lambda m: (m.status, m.key)):
        note = f"  [dim]{m.last_failure}[/dim]" if m.last_failure else ""
        console.print(f"{m.status:<9} {m.id}  {m.describe()}  "
                      f"[dim]seen {m.successes}x, failed {m.failures}x[/dim]{note}")


def _find_macro(store, macro_id: str):
    match = next((m for m in store.macros.values() if m.id == macro_id or m.key == macro_id), None)
    if match is None:
        raise click.ClickException(f"No macro matches {macro_id!r}.")
    return match


@macros.command("show")
@click.argument("macro_id")
@click.option("--config", "config_path", default="feena.yaml")
def macros_show(macro_id: str, config_path: str) -> None:
    """Inspect a shortcut by its ID or key, including its recorded steps."""
    m = _find_macro(_store(config_path), macro_id)
    console.print(f"{m.id} · {m.status} · {m.describe()}", markup=False)
    for i, step in enumerate(m.steps, 1):
        console.print(f"{i}. {step.describe()}", markup=False)


@macros.command("reset")
@click.argument("macro_id", required=False)
@click.option("--all", "reset_all", is_flag=True, help="Remove every stored shortcut.")
@click.option("--config", "config_path", default="feena.yaml")
def macros_reset(macro_id: str | None, reset_all: bool, config_path: str) -> None:
    """Remove a shortcut so agents discover it again, or explicitly reset all shortcuts."""
    if bool(macro_id) == reset_all:
        raise click.UsageError("Specify a macro ID or --all.")
    store = _store(config_path)
    if reset_all:
        n = len(store.macros)
        store.macros.clear()
    else:
        m = _find_macro(store, macro_id)
        del store.macros[m.key]
        n = 1
    store.save()
    console.print(f"Removed {n} shortcut(s).")


@macros.command("promote")
@click.option("--config", "config_path", default="feena.yaml")
@click.option("--min-successes", type=click.IntRange(min=1), default=None,
              help="Observations needed (default: macros.min_successes in feena.yaml).")
@click.option("--prune-days", type=click.FloatRange(min=0), default=30, show_default=True,
              help="Also drop stale macros and ones unseen for this many days.")
def macros_promote(config_path: str, min_successes: int | None, prune_days: float) -> None:
    """Nightly job: promote well-observed shortcuts so agents can use them; drop stale ones."""
    cfg = _load_or_default(config_path)
    from .macros import MacroStore
    store = MacroStore(cfg.macros_path)
    promoted = store.promote(min_successes or cfg.macros.min_successes)
    pruned = store.prune(prune_days)
    store.save()
    for m in promoted:
        console.print(f"[green]promoted[/green] {m.id}  {m.describe()}")
    console.print(f"{len(promoted)} promoted · {len(pruned)} pruned · "
                  f"{sum(m.status == 'promoted' for m in store.macros.values())} in use")


@macros.command("export")
@click.option("--config", "config_path", default="feena.yaml")
@click.option("--out", "out_dir", default=".feena/macro-tests", show_default=True)
def macros_export(config_path: str, out_dir: str) -> None:
    """Write each promoted macro as a standalone Playwright pytest (no model, no Feena)."""
    from .macros import export_pytest
    store = _store(config_path)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    n = 0
    for m in store.macros.values():
        if m.status == "promoted":
            (out / f"test_macro_{m.id}.py").write_text(export_pytest(m))
            n += 1
    console.print(f"Wrote {n} test(s) to {out}. Run with FEENA_BASE_URL=... pytest {out}")


if __name__ == "__main__":
    main()
