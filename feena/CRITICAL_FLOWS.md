# Critical-flow release suites

Keep the flows you manually retest in a shared, versioned checklist. Suites reference
existing deterministic browser scenarios; running a suite does not invoke AI agents.

Add to your `feena.yaml` after defining the referenced `scenarios`:

```yaml
suites:
  - name: release
    description: Essential browser flows before shipping
    scenarios: [login, onboarding, checkout]
```

```bash
feena suites --config feena.yaml
feena simulate --config feena.yaml --suite release --url http://localhost:3000
```

Suite membership order determines execution order. Every network profile configured on
each scenario runs. Choose either `--suite` or `--scenario`; omitting both retains the
existing behavior of running all scenarios. Unknown members, duplicate names, and empty
suites are rejected before the target opens.

Each execution saves a new directory under `.feena/suite-runs/` with `summary.json`,
`report.md`, and browser evidence. The summary includes profile results, evidence paths,
counts, completeness, and an aggregate status. The command exits nonzero for failed,
inconclusive, missing, duplicate, or unexpected results, so an incomplete run cannot pass
the release check. The latest report remains at `.feena/simulation-report.md`.

Use the same command as a CI step after starting your disposable app. Reports/evidence
stay local unless you explicitly configure your CI to upload them; they may contain
sensitive test data. Artifact paths are local paths, not hosted dashboard URLs.

Pair visible UI assertions with JSON assertions for persisted state. Reset or isolate
test data between profiles and runs; suites do not reset the database automatically.
This first milestone supports browser journeys, not native mobile or arbitrary device
events. Human UX review remains useful alongside repeatable regression checks.
