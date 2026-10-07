# Contributing to AwLPay

Thanks for helping make agent payments boring and honest.

## Ground rules

- **All money math is integer cents.** No floats, ever — `server/fees.py`
  is the law and `tests/test_awlpay.py` pins it.
- The fee law: free tier 0.5% flat per transaction, no fixed fee.
  A PR that changes the law says so in the PR body, loudly.
- A refusal is a result, not something to work around: unknown value,
  missing path, or dust-eaten-by-fees returns a machine-readable
  `{refused: true, reason}`, never a bad rate.
- Mainnet stays hard-disabled in this repo. Broadcast tests run on
  testnets only.

## Quick checks

```sh
python3 -m pytest tests/ -q        # the Python test suite
cd sdk && npm test                 # TypeScript SDK (fees + router)
```

CI runs both plus an API smoke test
(boot, `/healthz`, `/`, `POST /api/pay/quote`) on every pull request.

The two solana tests in `tests/test_chains.py` need the *optional*
`solders` dependency (commented out in `server/requirements.txt` by
design — mock mode and the suite run without it). CI deselects them.

## Changing a route

1. Edit `server/app.py` and update `openapi.yaml` to match.
2. Add or update tests in `tests/`.
3. Run the full suite. It must be green.
4. Open a pull request using the template.

## Licensing

AwLPay is Apache-2.0 (see LICENSE). By contributing you agree your
contribution may be distributed under that license.
