# Feena — realistic browser QA

Mallory tests browser journeys under real-world conditions: retries, interrupted connections,
lost responses, offline recovery, and multiple tabs. It pairs visible outcomes with backend
assertions, records evidence, and exposes configured tests to coding assistants through MCP.

The application lives in [`mallory/`](mallory/).

- [Features, local setup, and examples](mallory/README.md)
- [Hosted deployment and simple Cursor onboarding](mallory/DEPLOY.md)
- [Retry-safe checkout demo](mallory/examples/resilient-checkout/)

```bash
cd mallory
pip install -e '.[dev,mcp]'
playwright install --with-deps chromium
pytest -q
```

This is a single-workspace prototype, not yet autonomous user simulation or multi-tenant
production hosting. Hosted installation requires an operator-configured HTTPS service and
disposable test environment. Never commit access keys, traces, recordings, or production data.

The bundled vulnerable applications are deliberately insecure local testing fixtures.
