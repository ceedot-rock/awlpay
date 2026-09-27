# AwLPay Wallets

WIP wallets / settle surface for AwLPay. Built from a single CuNi FeeManager spec.

## Layout

| Path | What |
|------|------|
| `exact/` | **CuNi FeeManager SoT + Bank fixtures** (exactness or refuse) |
| `docs/fee-manager-spec.md` | Locked Free / Pro / L33t fee math |
| `openapi.yaml` | REST API sketch |
| `sdk/` | npm `@awlpay/sdk` (TypeScript stubs — **mirrors only**) |
| `mcp/server.py` | MCP tools: create wallet, balance, quote, settle — **mirrors only** |
| `assets/` | Brand mark |
| `web/` | Local wallets / settle demo UI |

## Fees (locked)

- **free:** 1.0% + $0.25 (`amount_cents * 10 / 1000 + 25`)
- **pro:** $39/mo · **0** only under **both** `$30k` volume **and** 500 txs/mo; else free formula as `pro_overage`
- **l33t:** $799/mo · unlimited platform fee 0
- **unknown tier:** refuse (`refuse=unknown_tier`) — no soft PASS

See `exact/FeeManager.cuni` for the CuNi law. `sdk/` and `mcp/` are thin mirrors of that SoT — Bank PASS is truth.

## Try the UI locally

Plain HTML/CSS/JS under `web/` — no build step, no npm publish.

```bash
cd web
python3 -m http.server 5173
```

Open [http://localhost:5173](http://localhost:5173). You’ll see wallets, fee quotes, and settle against local stubs. The yellow **Demo / stubs** badge means it’s not live money — `api.awlpay.com` isn’t up yet.

Fee math in the UI mirrors `sdk` `calculateFees` and `docs/fee-manager-spec.md`.

## Status

**SoT is `exact/`.** SDK + MCP are mirrors of `exact/FeeManager.cuni`. FeeManager + Bank landed (PR #2). **npm publish and Fly deploy: HOLD** — Cos GREEN on fee law only; do not publish or set Fly secrets yet.
