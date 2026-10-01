# Private hosted workspace

## What your users do

1. Open your Feena website.
2. Paste the workspace access key you gave them.
3. Click **Check connection**, then **Add to Cursor** and approve installation.
4. Ask Cursor: “Show me the available Feena tests.”

They do not install Python, run a terminal, or edit JSON. Cursor desktop must already be
installed. The installation link contains their access key: never share it or commit the
resulting Cursor configuration. This first release uses a shared workspace key, not individual
accounts. Use it only with a trusted team.

## One-time administrator setup

The repository includes a Dockerfile for a Linux container host. This is deployment preparation,
not a claim that a production service has been provisioned. The container build needs internet
access for Python dependencies and Chromium. Use one Linux service replica per campaign store; an exclusive lock rejects a second owner.

Configure these secrets/settings at your hosting provider:

| Setting | Value |
| --- | --- |
| `FEENA_MCP_TOKEN` | Random secret, at least 32 characters; share privately with authorized users |
| `FEENA_MCP_PUBLIC_URL` | Stable HTTPS endpoint, e.g. `https://qa.example.com/mcp` |
| `FEENA_MCP_HOSTS` | Exact public hostname, e.g. `qa.example.com` |

Terminate HTTPS at the hosting provider and forward to port 3000. The health route is `/health`.
Do not configure the temporary Hoplite preview URL as the public installation URL. The onboarding
page deliberately disables installation until the stable public URL is configured.

Mount the prepared `feena.yaml` at `/config/feena.yaml` read-only. Supply container arguments:

```
--config /config/feena.yaml --target http://test-app:5055
```

The target must be an authorized disposable app on the server's private network. The hosted
service cannot reach a user's laptop through `localhost`. An administrator still needs to
connect that test environment and prepare its browser journeys. Arbitrary public-site testing,
automatic journey generation, self-service customer accounts, and OAuth are not included.

Mount private persistent storage at `/data` writable by the `feena` container user, and set
CPU/memory limits and a retention policy. Browser traces can contain sensitive data. Give the
container only network access to its test environment. Do not mount a Docker socket or provider
credentials. Campaign state and scenario snapshots persist in SQLite under the output directory.
Limits: 1–16 worker slots, 120 seconds per browser job, 100 jobs per campaign, and 1,000
jobs per store. Archive the complete store and use a new output directory when full. This is a pilot, not unattended
multi-tenant production infrastructure.

## Before inviting users

- Confirm HTTPS, Host allowlist, token rejection, and health checks.
- Open the onboarding page; verify a wrong key fails and the correct key enables installation.
- Connect from Cursor desktop and run an approved journey against disposable data.
- Confirm cancellation and inspect the resulting traces privately.
- Rotate the workspace key by changing the secret and restarting if a key or install link leaks.

Cursor's install-link format: https://cursor.com/docs/mcp/install-links


## Parallel campaigns

The MCP service now exposes `start_campaign(scenarios)`, `get_campaign(campaign_id)`, and
`cancel_campaign(campaign_id)`. Existing `start_run`, `get_run`, and `cancel_run` calls use
the same durable queue. Every configured scenario/profile pair becomes one browser job.
Campaigns execute configured journeys and reviewed discovery proposals. Hostile-agent
scheduling is not included in this release.

Provision independent disposable copies of your app and database, then repeat `--target`
once per worker slot (up to 16). Different URLs must not be aliases for the same backend:
Feena cannot detect shared databases. Each slot executes only one job at a time across all
campaigns. Provide a private same-origin POST reset endpoint using `--reset-path` if tests
do not isolate their own data. The reset endpoint must finish resetting and seeding data
before returning 2xx; redirects or failures prevent the browser job from running.
Reset hooks are operator configuration, never supplied through MCP.

```bash
feena-mcp --transport http --config /config/feena.yaml --out /data/campaigns \
  --target http://test-app-1:5055 --target http://test-app-2:5055 \
  --reset-path /test/reset
```

The example reset route must be implemented by your test app; the bundled checkout fixture
instead isolates order data with a fresh browser-session cookie. Do not expose reset routes
on a production app. The server still needs the HTTPS/token settings above.

Call `start_campaign` with, for example:

```json
{"scenarios": ["retry-checkout", "broken-idempotency", "offline-recovery"]}
```

Poll `get_campaign` using the returned ID. It reports per-job IDs, scenario/profile names,
status, and sanitized execution reasons. A campaign is `completed` only when all assertions
pass; `failed` means at least one definite assertion failure, and `inconclusive` means an
execution error, timeout, or interrupted job requires investigation. `queued`, `running`,
`cancelling`, and `cancelled` describe scheduling state. The legacy run API retains its
`completed` execution status and exposes individual assertion outcomes in `results`.

Raw worker results, scenario snapshots, traces, screenshots, and manifests remain under
`<out>/<job-id>/`; keep this directory private. Cancellation stops queued work and kills
active worker process groups before the slot can be reused. It does not roll back backend
writes. Configure a reset hook or use journeys that isolate their own data.

### Restart and recovery

Completed results and queued scenario snapshots survive restart. The store is bound to its
original target list and reset path; a different configuration requires a new store.
If shutdown interrupted active work, startup fails closed. First stop the previous service
and all orphan worker/browser processes (recreate its container where applicable), wait for
outstanding backend work to stop, and reset every target. Then restart once with
`--recover-interrupted` to acknowledge that cleanup. Interrupted jobs remain inconclusive;
only queued jobs resume. Remove the flag from normal startup configuration: it is an
operator acknowledgement, not automatic recovery or a substitute for process cleanup.

This is bounded parallel execution on one host, not multi-tenant or distributed hosting.

## Discover a journey from a goal

Discovery uses a model to choose browser actions, then proposes a reusable scenario. The
operator supplies the goal and expected outcomes in `feena.yaml`; the model cannot author
its own definition of success. Configure a `discovery` entry such as the one now included
in `examples/resilient-checkout/feena.yaml`:

```yaml
discovery:
  - name: discovered-checkout
    goal: Place exactly one order and wait for its confirmation.
    start_path: /
    max_steps: 12
    timeout_seconds: 60
    assertions:
      - {kind: text, target: "#status", expected: "Order confirmed."}
      - {kind: json, target: /api/state, expected: {orders: 1}}
```

Set `ANTHROPIC_API_KEY` privately on the Feena service to enable discovery. It uses the
existing model integration and may incur provider charges. Page observations and the goal
are sent to that provider; use disposable test data. Missing credentials return an
inconclusive discovery result, never a passing test. Ordinary simulation workers do not
receive the provider key.

From Cursor:

1. Ask Feena to `list_discovery_goals`.
2. Call `start_discovery` with `{"goal": "discovered-checkout"}`.
3. Poll `get_discovery` using its `campaign_id`. Inspect the proposed steps and assertions.
4. After reviewing the journey, call `approve_discovery` with that same ID. This persists the
   scenario and queues an independent browser replay through the campaign runner.
5. Poll `get_campaign` for the returned replay campaign. A proposal is not a verified
   repeatable test until replay passes. After replay passes, the approved scenario appears in `list_scenarios`
   and can be used in later campaigns. Failed or incomplete validation does not promote it.

Discovery shares target slots, reset hooks, cancellation, persistence, and interrupted-run
recovery with simulation campaigns. Use `cancel_campaign` to stop an exploration. Browser
navigation and requests remain within the configured origin. Redirect responses and
WebSocket connections are blocked in discovery and replay; use direct test routes in this
release. Redirect-dependent sign-in flows are not supported yet. Discovery does not provision
accounts, solve CAPTCHAs, manage external identity providers, or generate adversarial tests.
It currently generates a normal-network journey; existing configured scenarios can supply
network fault profiles. Review generated values because proposals can contain synthetic
form inputs. Do not use real credentials or private production data in discovery goals.

This feature must be deployed from a repository/branch containing the discovery changes.
A Render service tracking another fork does not receive them automatically.
