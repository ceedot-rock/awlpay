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

## Fees (locked)

- **free:** 1.0% + $0.25
- **pro:** $39/mo · under $30k volume or 500 txs → $0 platform fee
- **l33t:** $799/mo · unlimited

## Status

Stubs only. No live `api.awlpay.com` yet. Do not publish npm until CoS green.
