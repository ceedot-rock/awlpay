## What changed

<!-- One or two sentences. -->

## Components touched

<!-- e.g. server/x402.py, sdk/src/fees.ts, openapi.yaml, or "docs only" -->

## Checks

- [ ] `python3 -m pytest tests/` passes (minus the two pre-existing
      solders-optional failures, if you did not install the optional dep)
- [ ] `cd sdk && npm test` passes, if any SDK source changed
- [ ] `openapi.yaml` updated for any route change
- [ ] Fee law unchanged (integer cents; free tier 0.5% flat, no fixed fee),
      or the law change is called out explicitly
