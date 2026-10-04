# AGENTS.md — awlpay

AwLPay: multi-rail x402 stablecoin payments for agents. Python + JS SDKs.

## Layout

- `py-sdk/` — Python SDK (`awlpay` on PyPI)
- `js-sdk/` — TypeScript SDK (`awlpay` on npm)
- `QUICKSTART.md` — the 5-minute path, keep it current

## Test

```bash
cd py-sdk && python -m pytest        # 21 tests
cd js-sdk && npm test               # 18 tests, plus tsc --noEmit
```

## Publish (Corey does this from his phone — Termux)

- Python: `cd py-sdk && python -m build && twine upload dist/*`
- JS: `cd js-sdk && npm run build && npm pack`, then `npm publish <tarball> --access public`
- The js-sdk `dist/` is gitignored — ship a prebuilt tarball on the branch
  (`awlpay-js-<version>.tgz`, `git add -f`) so Termux can publish without a build.
- Publish branch: `awlpay-sdk-publish`. It omits `.github/workflows/ci.yml`
  because the available GitHub token lacks `workflow` scope — do not re-add it
  here; CI lives on other branches.

## Gotchas

- `package.json` MUST carry `repository` + `homepage` — npm shows no GitHub
  link without them (we shipped 0.1.0 without; fixed in 0.1.1).
- Never publish without bumping the version — the registry rejects it and the
  error reads as "already live," which is correct: check registry state first.
- No real funds or mainnet writes in tests. Ever.
