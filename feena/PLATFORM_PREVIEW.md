# Dashboard, billing, and native runner preview

This builds on critical-flow suites. It is a local, single-workspace preview,
not a production multi-tenant paid service.

## Results dashboard

```bash
pip install -e '.[dashboard]'
# Configure a private random access key of at least 32 characters in your environment.
feena-dashboard --config feena.yaml --port 5056
```

Set `FEENA_DASHBOARD_TOKEN` before starting. Open http://127.0.0.1:5056 and enter
the key. It stays in browser memory, is cleared on disconnect/reload, and is never
placed in a URL. The server binds only to loopback.

The dashboard lists configured browser suites, the latest 100 saved suite runs,
per-profile statuses, screenshots, traces, and JSON evidence. Evidence endpoints require
authentication and expose only allowed files inside the selected run. Run suites using
the CLI; dashboard run launching is not implemented in this milestone. Historical files
must come from a trusted local runner. Do not expose this development server publicly.

## Stripe test plumbing

Configure these private environment variables:

- `FEENA_STRIPE_TEST_KEY`: Stripe `sk_test_...` key. Live keys are rejected.
- `FEENA_STRIPE_WEBHOOK_SECRET`: endpoint-specific `whsec_...` signing secret.
- `FEENA_STRIPE_PRICE`: test-mode recurring `price_...` ID.
- `FEENA_DASHBOARD_URL`: origin for Checkout/portal return URLs; defaults to local server.

The dashboard can create hosted test subscription Checkout sessions and customer portal
sessions. Forward subscription created/updated/deleted events to `/billing/webhook` with
the Stripe CLI. Verification uses the SDK on the raw body. Event IDs are deduplicated in
SQLite, and current subscription state is fetched from Stripe to handle out-of-order
delivery. Customer mapping is established by server-set subscription metadata; redirects
do not provision access. Live-mode events and subscriptions are rejected.

This stores billing state only. Production workspace authentication, usage metering,
atomic budget reservations, and enforcement across CLI/MCP/dashboard are still required
before charging customers. Subscription status is not currently an execution entitlement.
No prices, products, webhooks, or subscriptions are created until an operator configures
test credentials and explicitly uses the billing action.

## Native mobile runner

`feena-mobile` executes deterministic native flows through a locally installed Appium
server using Android UiAutomator2 or iOS XCUITest. Provide a simulator/emulator or
disposable device, the matching driver, and an app build. iOS execution requires a
compatible macOS/Xcode environment. Feena does not provision devices or install drivers.

Create a separate `mobile.yaml`:

```yaml
capabilities:
  platformName: Android
  appium:automationName: UiAutomator2
  appium:deviceName: Android Emulator
  appium:app: /absolute/path/to/test-build.apk
journeys:
  - name: login
    steps:
      - {action: fill, target: email, value_env: FEENA_TEST_EMAIL}
      - {action: fill, target: password, value_env: FEENA_TEST_PASSWORD}
      - {action: tap, target: login}
    assertions:
      - {kind: visible, target: home-screen}
```

Selectors default to accessibility IDs; `id` and `xpath` are supported. Actions are tap,
fill, and bounded wait; assertions are visibility and exact text. Required environment
values are resolved before opening a device session. Action metadata omits input values,
but screenshots can contain sensitive UI content and remain private local evidence.

```bash
feena-mobile --config mobile.yaml --server http://127.0.0.1:4723
```

Each journey gets a new device session and final screenshot. Session cleanup runs after
success and failure. Setup/action errors or missing required evidence are inconclusive;
assertion mismatches fail. Results use the same `.feena/suite-runs` summary format as
browser runs, so the dashboard can show them when using the same output directory.

This experimental adapter has protocol tests but has not been validated on a physical
device. It does not yet handle calls, background/foreground transitions, permission
dialogs, network injection, backend assertions, device pools, or automatic fixture resets.
