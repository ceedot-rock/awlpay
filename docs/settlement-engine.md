# SettlementEngine (mock / first cut)

Local in-process HTTP API. **No live Solana/Base/Ethereum transfers.** CoS gates Fly, npm publish, and production DNS.

## What it does

- In-memory `Map` of wallets keyed by UUID
- CuNi-locked `calculateFees` from `@awlpay/sdk` (exact integer cents)
- Mock settle: debit sender `amount + platform_fee`, credit receiver `amount`, append receipt

## Refuse rules (`POST /v1/settle` → 400)

| reason | when |
|--------|------|
| `wallet_not_found` | from or to missing |
| `invalid_amount` | `amount_cents <= 0` |
| `same_wallet` | from == to |
| `insufficient_funds` | from balance < amount + platform_fee (+ trading placeholder 0) |
| `missing_fields` | required body fields absent |

## DEV-ONLY funding

`POST /v1/wallets/{id}/credit` with `{ "amount_cents": N }` credits a wallet for local tests. Marked `mock: true` in the response. Not for production.

## Run

```bash
cd server && npm install && npm run build && npm start
# or: npm run dev
# default http://127.0.0.1:8787
```

Health: `GET /healthz` → `{ "ok": true }`
