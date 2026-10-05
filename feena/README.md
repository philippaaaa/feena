# Feena

**Connecting from Cursor?** Open your hosted Feena workspace and follow the three-step
setup page: access key → check connection → Add to Cursor. No terminal or JSON editing is
needed for end users. A temporary preview deliberately disables installation until a stable
HTTPS service is configured. See [DEPLOY.md](DEPLOY.md) for the one-time administrator setup
and the remaining pilot-hosting limitations.

**Agents that use your app, break your app, and attack your app — in a sandbox, on every pull request.**

AI writes most of the code now, and it writes most of the tests too. The tests are the
problem: they assert what the author already believed, they run against mocks instead of the
running system, and nothing in the suite double-submits, swaps an ID in the URL, or behaves
like an attacker. Coverage goes up while confidence goes down.

Feena boots your app the way production does, then sends three kinds of agent at it through a
real browser, with no access to your source or your test IDs:

| Agent     | Behaves like                                                            | Finds                                                       |
| --------- | ----------------------------------------------------------------------- | ---------------------------------------------------------- |
| `regular` | A new user following the core flows                                      | Broken signups, dead buttons, flows that fail end to end   |
| `clumsy`  | A distracted user: wrong input, double clicks, back button, two tabs     | State bugs, race conditions, validation gaps, data loss     |
| `hostile` | An attacker with a normal account                                        | Broken access control, missing auth, unescaped input, weak headers |

Every finding comes back with a screen recording, the exact steps, and a generated regression
test. **The promise: no finding without a reproduction.**

## Safety model (read this)

The `hostile` agent is a defensive security-testing tool, not an exploitation framework. Two
constraints are enforced structurally, not just documented:

1. **Sandbox only.** The hostile agent can only talk to the app under test, running inside a
   throwaway container Feena built. It never runs against a URL you point it at by hand.
2. **No egress.** The sandbox network has outbound access disabled, so an agent cannot reach
   anything but the target. Attacks can only point at the copy under test.

The checks are non-destructive and confirm by *observation* (e.g. "request user B's object as
user A and see whether B's data comes back"), never by damaging data. Feena will refuse to
run the hostile agent against a target it did not itself sandbox. See `sandbox.py`.

## Quick start

```bash
pip install -e .
playwright install chromium

# point Feena at how your app boots
cp feena.example.yaml feena.yaml
$EDITOR feena.yaml

# run all three agents against a fresh sandbox
feena run

# or one agent, headed, for debugging
feena run --agent regular --headed
```

Set `ANTHROPIC_API_KEY` for the exploratory agents (`regular`, `clumsy`). The `hostile`
agent's core checks are deterministic and run without a key; the LLM only helps it prioritise
where to look.

## Architecture

```
feena run
   │
   ├─ config.py     load feena.yaml (how the app boots, seed users, scope)
   ├─ sandbox.py    build + start the app in Docker on a no-egress network
   ├─ browser.py    Playwright session, one recorded context per agent run
   ├─ agents/
   │    base.py     the perceive → reason → act → evaluate loop
   │    regular.py  LLM-driven happy-path exploration
   │    clumsy.py   LLM-driven chaos: bad input, races, navigation abuse
   │    hostile.py  runs checks/ against the sandbox, LLM prioritises
   │    checks/     non-destructive, reproduction-first security checks
   ├─ findings.py   Finding model, dedup, and the replay-to-confirm step
   └─ reporter.py   Markdown / PR-comment output + saved regression tests
```

## GitHub Action: run it on every pull request

```yaml
# .github/workflows/feena.yml
on: pull_request
permissions: { contents: read, pull-requests: write }
jobs:
  feena:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: docker compose up -d --build --wait      # start YOUR app on the runner
      - uses: OWNER/feena@v0
        with: { url: "http://localhost:3000" }
```

No API key and no secrets. On each PR Feena attaches to your running app, attacks it with the
hostile agent, re-runs your **committed regression tests**, and posts **one** comment (updated in
place on every push, never a pile of them). The check fails on new problems, not old ones.

How a team adopts it, in order:

1. `feena run --url http://localhost:3000` locally to see what it finds.
2. `feena baseline` and commit `feena.baseline.json`. This accepts today's findings, so CI
   fails only on **new** ones. (Without it the first run fails forever and the check gets deleted.)
3. Add the workflow above.
4. As you fix bugs, `feena adopt --all` copies the generated tests into `tests/feena/`. Commit
   them. Adopt *after* fixing: a committed test for a bug that still exists fails CI by design.
5. Re-run `feena baseline` occasionally to prune entries that are now fixed. The comment tells
   you when some are.

| Input | Default | |
| --- | --- | --- |
| `url` | required | Your running app. Only loopback/private addresses are accepted. |
| `fail-on` | `high` | Fail on NEW findings at or above `critical/high/medium/low`, or `none`. |
| `tests` | `tests/feena` | Committed regression tests. Any failing one fails the check. |
| `baseline` | `feena.baseline.json` | Known findings that don't fail the check. |
| `config` | `feena.yaml` | Seeded test accounts for the login-based checks. |
| `comment` | `true` | Post/update the PR comment. |

Outputs: `new-findings`, `regression-failures`, `failed`. Also writes the job summary, and uploads
the report and generated tests as an artifact.

Things worth knowing:

- **Only ever your own copy.** `attach` resolves the hostname and refuses anything that isn't a
  loopback or private address, with no override. Point it at a test database with seeded test
  accounts, never production data.
- **Fork PRs** get a read-only token, so the comment is skipped; the job summary carries the same
  content. Don't use `pull_request_target` to work around that, since it would run untrusted code
  with your secrets.
- Workflow inputs reach the shell only through `env:`, never interpolated into the script, and the
  regression tests run without your CI token in their environment.
- Findings that appear in the baseline are hidden, so treat the baseline like a debt list, not a
  mute button. A finding that comes back after you've adopted its test fails both ways.

## Regression tests that actually run

Every confirmed finding is compiled into a real pytest that re-runs its reproduction and asserts
the bug is gone. Commit them next to your code: **fix the bug and the test passes; reintroduce it
and the test fails on the PR that broke it.** No API key, no LLM, no Feena install needed to run
them, only `httpx` and `pytest` (plus `playwright` for the few that exercise client-rendered
pages).

```bash
feena run                                   # writes .feena/tests/test_<fingerprint>.py
feena regress --base-url http://localhost:3000
# or, in your own CI:  FEENA_BASE_URL=... pytest .feena/tests
```

Proven end to end on TaskFlow (`tests/test_regress.py`): 9 generated tests are all red against
the vulnerable app, all green against `examples/taskflow-fixed`, and exactly one goes red when
only the IDOR fix is reverted. A Feena re-scan of the fixed app finds nothing.

Guard rails: the tests refuse any non-local target unless you set `FEENA_ALLOW_REMOTE=1`
(only ever for an environment you own or are authorised to test); credentials are read from
`FEENA_USER1_EMAIL` / `_PASSWORD` and never written into a test; a test asserts the *absence of
the bug*, not that the feature works; and a test that cannot exercise its reproduction fails
loudly rather than passing vacuously. Findings without a machine-readable reproduction (e.g. from
third-party plugins) are listed in the report as "verify manually" rather than given a fake test.

## Verification and positive user outcomes

Confirmation replays the finding's machine-readable reproduction and checks the reported
condition again. A screenshot or a successful HTTP response alone is not confirmation.
`feena run` and `feena ci` save per-candidate statuses and reasons in
`.feena/verification.json`: `reproduced`, `not_reproduced`, or `inconclusive`. Unsupported
reproductions (including exploratory browser findings without a replay spec), missing setup,
and unavailable replay dependencies must not become confirmed findings. A non-reproduction
is not proof of a fix or complete coverage.

Security regressions answer **“is the bug gone?”** Add explicit `outcomes` to answer
**“can the user still do the job?”** For example, an IDOR regression can pass when a task
endpoint is removed; the following companion journey cannot:

```yaml
outcomes:
  - name: own-task-update
    user: 1
    steps:
      - path: /api/tasks/1
        expected_json: {id: 1, owner_id: 1}
      - method: PATCH
        path: /api/tasks/1
        json_body: {title: Updated by Feena}
        expected_json: {id: 1}
      - path: /api/tasks/1
        expected_json: {id: 1, owner_id: 1, title: Updated by Feena}
```

Adapt these routes, IDs, and fields to **disposable seeded data in your app**; the bundled
TaskFlow app has a read endpoint but does not implement this PATCH API. Outcome steps execute
in order in one session. `GET` and HTTP 200 are the defaults; supported methods are GET, POST,
PUT, and PATCH, with an explicit 2xx `expected_status`. Each step must assert a nonempty
`expected_json` object (nested object subsets are supported; arrays and scalar values match
exactly). Include a final read to detect an update that claims success but does not persist.

`user` selects the first or second seeded account in `users`; `null` means anonymous.
Authentication uses the same cookie-login conventions as the existing checks (`/api/login`,
`/login`, or `/api/auth/login`, with email/password). It then requires HTTP 200 from
`identity_path` (default `/api/me`), with a JSON `email` matching the selected seeded account;
a cookie alone does not prove authentication. Other authentication mechanisms are not yet
supported. Requests stay target-relative and redirects are not followed. These checks
can mutate data: reset fixtures between runs and do not use production accounts. Keep secrets
out of request payloads and expected values; these values are copied into generated tests.

- `feena run` and `feena ci` execute configured outcomes and include their results in the
  report. Failed or inconclusive outcomes fail the command, even with `--fail-on none` in CI;
  finding baselines cannot hide a broken user outcome.
- Outcome tests are generated alongside bug regressions. `feena adopt --all` copies both
  kinds and their helpers. They run with `feena regress` or standalone pytest, without an LLM
  or Feena installation. Credentials still come from environment variables, never generated
  source. Missing outcome credentials/setup fail rather than silently skipping.
- Existing configurations without `outcomes` retain their behavior. Outcomes are explicit
  HTTP checks, not an autonomous scenario planner or full browser replay engine.

The paired live-app tests in `tests/test_outcomes.py` cover a working fix, a reintroduced IDOR,
a removed endpoint, and an update that does not persist. Both security and usability matter:
neither assertion replaces the other.

## Browser UX simulation (first milestone)

Run a browser journey across concrete failure conditions, independently of the security
scanner and without an API key:

```bash
pip install -e '.[dev]'
playwright install chromium
python examples/resilient-checkout/app.py
# In another terminal (use the port printed by the demo):
feena simulate --config examples/resilient-checkout/feena.yaml --url http://localhost:5055
```

The example deliberately includes a broken checkout: the full command exits 1 because the
UI reports success but the backend creates two orders after a retry. To run only the resilient
checkout, add `--scenario retry-checkout`; all four network profiles should pass.

`scenarios` are also executed by `feena run` and `feena ci`. Set `run.agents: []` to
focus on UX simulations without running security or exploratory agents. Any failed or
inconclusive simulation fails CI, independently of `--fail-on` and finding baselines.

Each scenario supplies a user goal, browser actions, explicit outcome assertions, and network
profiles. This milestone is deterministic scripted simulation, **not yet autonomous persona
planning or a concurrent multi-user swarm**. It provides the execution and measurement layer
those agents will need.

Profiles use actual Playwright request interception:

| Effect | Behavior |
| --- | --- |
| `normal` | No injected fault |
| `delay` | Delay the selected request before sending it |
| `abort` | Prevent the selected request from reaching the server |
| `drop_response` | Send the request, then discard its response before the browser receives it |

`path` is a URL-path prefix, `method` optionally narrows requests, and `occurrence` selects
the matching request number (default 1). The fault applies once, allowing a subsequent retry
to succeed. An untriggered fault is **inconclusive**, not evidence that the app recovered.
These are targeted fault injections, not a complete Wi-Fi/bandwidth simulation.

Available actions: `goto`, `click`, `fill`, `press`, `back`, `reload`, `new_tab`, `switch_tab`,
`offline`, `online`, and bounded `wait`. Use CSS or Playwright selectors, including
`role=button[name="Buy"]`. New/switch-tab actions select the active tab; subsequent actions
and UI assertions use that tab. Tabs share cookies within a profile. `offline`/`online` toggle
the browser context's connectivity. Viewport width/height are configurable; a narrow viewport
does not emulate a real phone, touch input, or mobile operating system.

Assertions support visible elements, text, element count, and JSON response subsets. The JSON
check uses the browser context's authenticated request client to inspect persisted state,
outside the injected browser fault. Pair UI feedback with a backend invariant such as
`{orders: 1}`: a success message alone cannot detect duplicate purchases.

Each profile gets a fresh browser context and a unique artifact directory with `trace.zip`,
screenshots, `actions.json`, and a versioned `manifest.json`. The application database is **not
reset automatically**. Seed/reset disposable fixtures or use per-session data isolation, as
the demo does. Artifacts may contain credentials, page contents, and submitted values; they
remain local and are not automatically uploaded by the GitHub Action.

After restoring the target's starting data, rerun exactly the recorded journey/profile:

```bash
feena replay-simulation path/to/manifest.json --url http://localhost:5055
playwright show-trace path/to/trace.zip
```

Replay re-executes the scenario, not a database snapshot or a timing-exact recording. A failed
simulation is an observed failed journey—not automatically a confirmed product bug. Inspect
the evidence and reproduce from known state before attributing it to the app.

Only authorized local/private targets are accepted by the CLI. Browser HTTP requests outside
the configured origin are blocked and service workers are disabled for deterministic routing;
apps depending on external authentication, CDNs, or service workers require a suitable local
fixture. This is not a hardened hosted execution boundary and does not isolate arbitrary
application code or every networking mechanism.

## MCP server (single-workspace preview)

Install `pip install -e '.[mcp]'` and `playwright install chromium`. The MCP service exposes
`list_scenarios`, `start_run`, `get_run`, `cancel_run`, `start_campaign`, `get_campaign`, and
`cancel_campaign` using the official Python MCP SDK.
Runs execute asynchronously in separate worker processes; poll `get_run` until completed,
cancelled, timed_out, or error. A completed run can contain failed or inconclusive profiles.

For a local coding tool supporting stdio MCP, configure:

```json
{
  "mcpServers": {
    "feena": {
      "command": "/absolute/path/to/venv/bin/python",
      "args": ["-m", "feena.mcp_server", "--config", "/absolute/path/to/feena.yaml",
               "--target", "http://127.0.0.1:5055"]
    }
  }
}
```

The target must already be running, and its URL is relative to the MCP **server's** network,
not the calling agent's computer. Only operator-configured scenarios can run. Clients cannot
provide target URLs, filesystem paths, scripts, or arbitrary new scenarios.

For a remote client that supports Streamable HTTP **and custom Authorization headers**:

```bash
# Set a random secret via your host's secret manager; never commit it.
export FEENA_MCP_HOSTS=mcp.your-domain.example
feena-mcp --transport http --host 0.0.0.0 --port 3000 \
  --config /srv/feena.yaml --target http://127.0.0.1:5055
```

Set `FEENA_MCP_TOKEN` to a random secret of at least 32 characters before starting HTTP.
Put the service behind HTTPS. Connect to `https://mcp.your-domain.example/mcp` with
`Authorization: Bearer <your secret>`. Set the exact served Host value in
`FEENA_MCP_HOSTS` (comma-separated if needed); host validation remains enabled. `/health`
is public and reveals only service availability. This is static bearer authentication,
**not OAuth**, so OAuth-only MCP clients are not supported yet.

The hosted service uses a durable SQLite campaign queue shared by the campaign and legacy
run APIs. Configure 1–16 isolated worker targets, with one active browser job per slot, a
120-second budget per job, up to 100 jobs per campaign, and 1,000 jobs per store. Results and
queued scenario snapshots survive restarts. Interrupted active work requires operator cleanup
and explicit recovery; see [DEPLOY.md](DEPLOY.md) before restarting an interrupted service.

Campaigns report `completed` only when all assertions pass; `failed` indicates an assertion
failure and `inconclusive` indicates execution uncertainty. The legacy run API retains its
execution-oriented `completed` status, so inspect its individual `results` as well.
Cancellation kills the worker/browser process group but cannot undo application writes.
Evidence remains under `.feena/mcp` until the operator deletes it. Keep that directory private
and apply a disk/retention policy. Raw traces/screenshots are not returned over MCP. The same
bearer token grants access to all runs: this is **single-workspace**, not multi-tenant
production hosting. Optional `FEENA_WORKSPACE_NAME` and `FEENA_ENVIRONMENT_NAME` display labels
help users confirm their workspace after key verification; never put secrets in these labels.

`scripts/serve_mcp_demo.py` starts the checkout fixture and HTTP MCP service for a managed
preview. Its generated token is stored locally at `.feena/mcp-token` with mode 0600 and is
not packaged with the source. A workspace preview has additional platform access controls
and expiring URLs; it is not a permanent connector address. A production deployment still
needs a stable HTTPS host, configured secrets, process supervision, and isolated test targets.

## Benchmark: test many apps at once

`feena bench` runs the hostile agent across a list of apps (`benchmark/targets.yaml`), each
cloned or pulled into its own no-egress sandbox, and writes one aggregated report. The default
targets are the OWASP vulnerable apps (Juice Shop, NodeGoat, DVWA) plus the bundled examples.
The harness only runs apps locally — never a hosted instance you don't own. See `benchmark/`.

## Attestations, corpus, and plugins

**Signed attestations.** `feena keygen` creates an Ed25519 keypair. With `attest.enabled`, each
run writes `attestation.json` (a signed record binding repo, commit, time, agents and every
confirmed finding) and `attestation.md` (a readable report with OWASP categories and suggested
SOC 2 / ISO 27001 control references). `feena verify attestation.json --pub feena_signing.pub`
fails if one byte changed. Control mappings are suggestions for filing evidence, not a certification.

**Findings corpus (opt-in).** With `corpus.enabled`, confirmed findings are appended to
`.feena/corpus.jsonl` as patterns only: kind, severity, check name, generalised route
(`/api/orders/:id`), stack. No URLs, hosts, bodies, input values, or repo names. Nothing leaves
your machine unless you run `feena corpus upload` against an endpoint you configured.
`feena corpus stats` shows your most frequent verified bug patterns.

**Check plugins.** Extra checks register through the `feena.checks` entry-point group and run
under the same sandbox guard as the built-ins, so check packs can ship separately from the runner.

## Status

Early. The scaffold, sandbox orchestration, findings/verification model, reporter, and the
`hostile` checks are implemented and runnable. The exploratory agents (`regular`, `clumsy`)
have a working loop and need iteration against real targets. See `feena.example.yaml` for
the first supported stack (Next.js + Postgres).

## License

Apache-2.0 (intended). The runner is free and open source; hosted runs and support are the
business.

## Reusable navigation, model routing, and change-aware exploration

Exploratory `regular` and `clumsy` agents can now choose from a numbered menu of actual
page controls. Accessible roles and full names identify each control; duplicate names use
an ordinal. This reduces invented selectors and supports double-click, fill, key presses,
back navigation, and recorded shortcuts. The modern Playwright ARIA snapshot is supported.
These features apply to the CLI's exploratory agents; the hosted discovery tools retain
their existing goal/proposal/replay workflow.

### Reuse reviewed navigation shortcuts

Enable recording in `feena.yaml`:

```yaml
macros:
  enabled: true
  path: ./.feena/macros.json
  min_successes: 3
```

The regular agent records routes it successfully reaches. The clumsy agent may reuse
promoted shortcuts but does not teach shortcuts from its irregular actions. Recording is
opt-in. Inspect the captured steps, then promote candidates observed repeatedly:

```bash
feena run --config feena.yaml --url http://localhost:3000 --agent regular
feena macros list --config feena.yaml
feena macros show <macro-id> --config feena.yaml
feena macros promote --config feena.yaml
feena macros export --config feena.yaml
```

Promoted shortcuts replay without model calls, validate their ending page structure, and
become stale if controls or the destination change. Shortcuts through routes affected by the
current Git diff are withheld so those pages receive fresh exploration. `feena macros reset
<macro-id>` removes one shortcut; `feena macros reset --all` clears the cache.

Credential fills use references such as `user1_password`, resolved from configured test
accounts at execution time. Unknown literal fills and paths containing query parameters are
not memoized. Keep browser traces private; they can still contain entered test data. Review
shortcuts before promotion because replay repeats every captured action, including writes.
Exported tests resolve credentials from `FEENA_USER1_EMAIL` / `FEENA_USER1_PASSWORD` and use
`FEENA_BASE_URL` for the target.

### Try a cheaper decider before escalating

```yaml
models:
  smart: claude-sonnet-4-6
  fast: null
  escalate_below: 0.6
```

Set `fast` to a supported cheaper Claude model ID or an installed `feena.deciders` plugin
name. Confident valid actions use the fast answer; low confidence, malformed answers,
provider failures, unsupported targets, and finding reports escalate to the smart model.
Leaving `fast` unset preserves the single-model behavior. This changes which model makes
exploratory decisions; it does not guarantee a particular cost reduction.

### Focus on routes affected by a change

```bash
feena blast-radius --repo . --base origin/main
feena blast-radius --repo . --base origin/main --json
feena run --config feena.yaml --url http://localhost:3000 --base origin/main
feena ci --config feena.yaml --url http://localhost:3000 --base origin/main
```

The analyzer follows imports to routes in supported frontend layouts and Python route
handlers, reports chains connecting changed files to pages, and includes deleted/renamed
files. Global changes widen exploration and invalidate all shortcut offers. Unmapped files
remain visible as uncertain coverage; route hints do not remove configured scenarios,
outcomes, or regression tests from the run. Agents receive route hints, not source files.

The GitHub Action accepts an optional `base` input. Fetch sufficient Git history first
(`actions/checkout` with `fetch-depth: 0`). Reports include `blast-radius.json` and
`run-stats.json` with model decisions, replayed shortcuts, saved steps, and stale shortcuts.

Exploratory browser sessions currently block HTTP redirects to keep redirect chains within the target boundary. Login journeys that require server redirects are unsupported until guarded redirect handling is implemented.
