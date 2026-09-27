# AwLPay Wallets

WIP wallets / settle surface for AwLPay. Built from a single CuNi FeeManager spec.

## Layout

| Path | What |
|------|------|
| `exact/` | **CuNi FeeManager SoT + Bank fixtures** (exactness or refuse) |
| `docs/fee-manager-spec.md` | Locked Free / Pro / L33t fee math |
| `openapi.yaml` | REST API sketch |
| `sdk/` | npm `@awlpay/sdk` (TypeScript stubs) |
| `mcp/server.py` | MCP tools: create wallet, balance, quote, settle |
| `assets/` | Brand mark |

## Fees (locked)

- **free:** 1.0% + $0.25 (`amount_cents * 10 / 1000 + 25`)
- **pro:** $39/mo · **0** only under **both** `$30k` volume **and** 500 txs/mo; else free formula as `pro_overage`
- **l33t:** $799/mo · unlimited platform fee 0

See `exact/FeeManager.cuni` for the CuNi law. Stubs in `sdk/` / `mcp/` are mirrors only — Bank PASS is truth.

## Status

FeeManager exact draft in `exact/`. No live `api.awlpay.com` yet. **Do not publish npm / set Fly secrets until Cos GREEN.**
