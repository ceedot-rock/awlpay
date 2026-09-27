# AwLPay Wallets

WIP wallets / settle surface for AwLPay. Built from a single CuNi FeeManager spec.

## Layout

| Path | What |
|------|------|
| `exact/` | **CuNi FeeManager SoT + Bank fixtures** (exactness or refuse) |
| `docs/fee-manager-spec.md` | Locked Free / Pro / L33t fee math |
| `docs/settlement-engine.md` | Mock SettlementEngine + refuse rules |
| `openapi.yaml` | REST API (OpenAPI 3.0.3) |
| `server/` | Local SettlementEngine HTTP API (TypeScript) |
| `sdk/` | npm `@awlpay/sdk` (TypeScript) — shared `calculateFees` mirror |
| `mcp/server.py` | MCP tools: create wallet, balance, quote, settle — **mirrors only** |
| `assets/` | Brand mark |
| `web/` | Local wallets / settle demo UI |

## Fees (locked)

- **free:** 1.0% + $0.25 (`amount_cents * 10 / 1000 + 25`)
- **pro:** $39/mo · **0** only under **both** `$30k` volume **and** 500 txs/mo; else free formula as `pro_overage`
- **l33t:** $799/mo · unlimited platform fee 0
- **unknown tier:** refuse (`refuse=unknown_tier`) — no soft PASS
- **trading_fee_cents:** `0` placeholder until real trading fee

See `exact/FeeManager.cuni` for the CuNi law. `sdk/` / `mcp/` / `server/` mirror it — Bank PASS is truth.

## Local server

```bash
cd server
npm install
npm test
npm run dev    # http://127.0.0.1:8787
```

Routes: `POST /v1/wallets`, `GET /v1/wallets/{id}/balance`, `POST /v1/wallets/{id}/credit` (DEV-ONLY mock), `POST /v1/quote`, `POST /v1/settle`, `GET /healthz`.

Point the SDK / MCP at it with `AWLPAY_BASE_URL=http://127.0.0.1:8787`.

## Try the UI locally

Plain HTML/CSS/JS under `web/` — no build step, no npm publish.

```bash
cd web
python3 -m http.server 5173
```

Open [http://localhost:5173](http://localhost:5173). You’ll see wallets, fee quotes, and settle against local stubs. The yellow **Demo / stubs** badge means it’s not live money — `api.awlpay.com` isn’t up yet.

Fee math in the UI mirrors `sdk` `calculateFees` and `docs/fee-manager-spec.md`.

## Status

**SoT is `exact/`.** First-cut SettlementEngine local (in-memory mock). SDK + MCP are mirrors of `exact/FeeManager.cuni`. **npm publish and Fly deploy: HOLD** until CoS green.
