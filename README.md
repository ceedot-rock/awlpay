# AwLPay Wallets

WIP wallets / settle surface for AwLPay. Built from a single CuNi FeeManager spec.

## Layout

| Path | What |
|------|------|
| `openapi.yaml` | REST API sketch |
| `sdk/` | npm `@awlpay/sdk` (TypeScript) |
| `mcp/server.py` | MCP tools: create wallet, balance, quote, settle |
| `docs/fee-manager-spec.md` | Locked Free / Pro / L33t fee math |
| `assets/` | Brand mark |
| `web/` | Local wallets / settle demo UI |

## Fees (locked)

- **free:** 1.0% + $0.25
- **pro:** $39/mo · under $30k volume or 500 txs → $0 platform fee
- **l33t:** $799/mo · unlimited

## Try the UI locally

Plain HTML/CSS/JS under `web/` — no build step, no npm publish.

```bash
cd web
python3 -m http.server 5173
```

Open [http://localhost:5173](http://localhost:5173). You’ll see wallets, fee quotes, and settle against local stubs. The yellow **Demo / stubs** badge means it’s not live money — `api.awlpay.com` isn’t up yet.

Fee math in the UI mirrors `sdk` `calculateFees` and `docs/fee-manager-spec.md`.

## Status

Stubs only. No live `api.awlpay.com` yet. Do not publish npm until CoS green.
