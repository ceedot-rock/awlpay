# awLPay v1

**awLPay** is a payment product from Slid Phi Labs: **any chain, any token — if it has verifiable value.**

The law: a conversion is only quoted when **both** tokens pass a `hasValue()` price check,
a priced path exists between them, and the fee math survives. Otherwise the quote
**refuses with a reason** — never guesses, never inflates. Coins are routed through
CuNi: the exact law everywhere.

## Architecture

```
                    ┌─────────────────────┐
                    │   POST /api/pay/quote   (free)
                    │   POST /api/pay/execute (402-gated)
                    └──────────┬──────────┘
                               ▼
                     ┌─────────────────────┐
                     │ 1. hasValue()       │  oracle.py — CoinGecko or Mock.
                     │    both tokens      │  Unpriced token → refuse.
                     │    priced?          │  (centralized trust assumption —
                     └──────────┬──────────┘   interface is swappable)
                               ▼
                     ┌─────────────────────┐
                     │ 2. ConverterRouter  │  router.py — BFS over a static
                     │    find_path()      │  chain/token graph. Bridge hops
                     │                     │  (same token, cross-chain),
                     └──────────┬──────────┘  swap hops (token pairs,
                               ▼             same chain). No priced path →
                     ┌─────────────────────┐  refuse.
                     │ 3. FeeManager       │  fees.py — integer-cents fee law,
                     │    calculate_fees() │  machine-verified against the
                     │    all 3 tiers      │  CuNi spec (18 fixtures).
                     └──────────┬──────────┘  Fee ≥ amount → refuse (dust).
                               ▼
                     ┌─────────────────────┐
                     │ 4. execute_quote()  │  settlement.py — computes
                     │    + attested       │  net = amount − fee, signs an
                     │    receipt          │  Ed25519 receipt attesting to
                     └─────────────────────┘  the LAW that would execute.
```

Fee math lives twice and must agree: `spec/FeeManager.cuni` (the law) and
`server/fees.py` (the Python mirror). The 18 fixtures in
`spec/fixtures/fees.json` are machine-verified against the CuNi side;
`tests/test_crosscheck.py` replays every fixture through the Python side.

## Fee tiers (locked 2026-09-27)

Tiers: `0` = free, `1` = pro, `2` = l33t. All arithmetic is **integer cents** —
no floats, no rounding ambiguity.

| Tier | Name | Price | Rule |
|------|------|-------|------|
| 0 | Free | pay-per-tx | **1.0% + $0.25/tx**: `fee = amount_cents // 100 + 25` |
| 1 | Pro | **$39/mo** | Cap **$30k volume OR 500 txs/mo**. Under cap: `fee = 0` (`pro_under_cap`). Over either cap: falls back to free pricing (`pro_overage`). Boundary: under iff `volume_used + amount <= 3_000_000` **and** `txs_used < 500`. |
| 2 | L33t | **$799/mo** | Unlimited, `fee = 0` (`l33t`). Fair-use compute guard at 100k txs/mo: `txs_used > 100_000` → `l33t_fair_use_review` (fee still 0, flagged for review). |

Status strings returned by `calculate_fees`: `free`, `pro_under_cap`, `pro_overage`,
`l33t`, `l33t_fair_use_review`, `refused_negative_amount`. (CuNi additionally has
`refused_unknown_tier` — Python **raises** `ValueError` on unknown tier instead;
see `tests/test_crosscheck.py` skip policy.)

Examples (from verified fixtures):
- `$100.00` free tier → `10_000 // 100 + 25 = 125¢` fee, net `9875¢`
- `$100.00` pro tier, fresh usage → `0¢` fee (`pro_under_cap`)
- `$100.00` pro tier, `volume_used = 2_999_999` (so `+10_000` exceeds $30k) →
  `125¢` (`pro_overage`)
- negative amount on any tier → `(0, "refused_negative_amount")`

## API reference

Base: `http://127.0.0.1:8899` (see `AWL_PORT`). All refusals are **HTTP 200**
with `{"refused": true, "reason": ...}` — machine-readable, not errors.

### POST /api/pay/quote — free

Validates a conversion: hasValue on both tokens, router path, fee math for
**all three tiers**. Refuses with a reason when the law fails.

Request:
```json
{
  "from_chain": "ethereum", "from_token": "ETH",
  "to_chain": "base",       "to_token": "USDC",
  "amount_cents": 10000, "tier": 0,
  "volume_used_cents": 0, "txs_used": 0
}
```
(`tier` defaults to 0; `volume_used_cents`/`txs_used` default to 0.)

Success response (200):
```json
{
  "path": [
    {"chain": "ethereum", "token": "ETH",  "hop": "origin"},
    {"chain": "ethereum", "token": "USDC", "hop": "swap"},
    {"chain": "base",     "token": "USDC", "hop": "bridge"}
  ],
  "fees": {
    "free": {"fee_cents": 125, "status": "free",          "net_cents": 9875},
    "pro":  {"fee_cents": 0,   "status": "pro_under_cap", "net_cents": 10000},
    "l33t": {"fee_cents": 0,   "status": "l33t",          "net_cents": 10000}
  },
  "tier": "free", "net_cents": 9875, "amount_cents": 10000
}
```

Refusal reasons (200, `{"refused": true, ...}`):
- `unknown_token_no_price` — either token has no verifiable price
  (`{"refused": true, "reason": "unknown_token_no_price",
    "detail": "DOGE on ethereum has no verifiable price"}`)
- `no_conversion_path` — both tokens priced but no priced route connects them
  (e.g. intermediate hops unpriced, or unknown chain/token in the router graph)
- `dust_eaten_by_fees` — fee ≥ amount on the requested tier
  (`{"refused": true, "reason": "dust_eaten_by_fees",
    "detail": "fee 25¢ >= amount 10¢ on tier free", "fees": {...}}`)
- `refused_negative_amount` — negative `amount_cents`
- `bad_request` — malformed JSON, missing/invalid fields
  (e.g. `tier` not in 0/1/2)

### POST /api/pay/execute — 402-gated

Route price: **25¢ flat** per execution (v1). Unpaid requests get **HTTP 402**
with an x402-style `paymentRequirements` body and a `PAYMENT-REQUIRED: 1`
header; pay (test mode), then retry.

Unpaid (402) — response body is the x402 envelope:
```json
{
  "x402Version": 1,
  "error": "payment required: pay 25 cents, then retry with X-Test-Payment: ok (test mode only)",
  "accepts": [{
    "scheme": "exact", "network": "eip155:8453",
    "maxAmountRequired": "25", "price_cents": 25,
    "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    "payTo": "0x0000000000000000000000000000000000000000",
    "resource": "https://127.0.0.1:8899/api/pay/execute",
    "description": "awLPay v1 /api/pay/execute — 25 cents flat per execution (route price)",
    "mimeType": "application/json", "maxTimeoutSeconds": 300
  }]
}
```

Paid (test mode — `AWL_TEST_MODE=1` + header `X-Test-Payment: ok`), same body
as `/quote`, 200:
```json
{
  "ok": true,
  "charged_cents": 25,
  "route_price_cents": 25,
  "attestation": {
    "alg": "ed25519",
    "kid": "b521ad84a17fe68f",
    "payload": "{\"amount_cents\":10000,\"fee_cents\":125,\"from_chain\":\"ethereum\",...\"mode\":\"mock\",\"net_cents\":9875,...}",
    "sig": "3363edcccacada5b…"
  }
}
```

The attestation payload is canonical JSON of the receipt:
`{receipt_id, from_chain, from_token, to_chain, to_token, amount_cents,
fee_cents, net_cents, tier, status, path, mode}` — verify with
`server.chamber.verify_attestation(attestation, verify_key)` (False on any
tamper; never raises). A reused `idempotency_key` gets **409**
(`{"error": "replay: idempotency_key already used"}`).

### GET /health

```json
{"ok": true, "mode": "mock-local"}
```
(`mode` comes from `AWL_MODE`; default `mock-local`.)

### Environment

| Var | Default | Meaning |
|-----|---------|---------|
| `AWL_PORT` | `8899` | listen port |
| `AWL_ORACLE` | mock | `coingecko` for live prices, anything else = MockOracle |
| `AWL_TEST_MODE` | `0` | `1` enables the `X-Test-Payment` gate on /execute |
| `AWL_RELAYER_KEY` | (ephemeral) | 64-hex-char Ed25519 seed; unset → per-process test key |
| `AWL_MAINNET_ENABLED` | `0` | `1` → settlement refuses loudly (no mainnet in v1) |
| `AWL_MODE` | `mock-local` | reported in /health |

## HONEST LIMITS

Read this before believing anything about awLPay. Nothing here is softened.

- **Settlement is MOCK. Coins never move.** `execute_quote()` runs the fee law
  and signs a receipt attesting to the law that *would* execute — the
  attestation is a receipt for a rule, not a movement of funds. `mode: "mock"`
  is on every receipt.
- **No mainnet.** `AWL_MAINNET_ENABLED=1` raises loudly; mainnet execution is
  not implemented in v1 and no real funds may touch this code path.
- **The 402 gate is test-mode only.** `X-Test-Payment: ok` is a header, not a
  payment. No real payment rail is wired; outside test mode every unpaid
  request 402s.
- **Chamber key custody is env-var/ephemeral.** The relayer seed comes from
  `AWL_RELAYER_KEY`; unset means a per-process ephemeral key (no trust).
  Chamber HSM custody is a marked TODO in `server/chamber.py` — production
  signing must happen inside the Chamber with key-id references only.
- **CoinGecko is a centralized trust assumption.** `hasValue()` is only as
  honest as the feed. The `PriceOracle` interface is swappable
  (`server/oracle.py`); the default offline oracle is a `MockOracle`.
- **ASGI server, no rate limiting yet.** Starlette + uvicorn (replaced the
  old stdlib `http.server` in workstream 4). Real rate limiting and
  persistent state are still TODO before adversarial traffic.
- **CuNi `cuni check` passes on 5 seats on this VM: py/js/ts/c/cpp.**
  Bare `cuni check` tries all 144 catalog seats: the go/rs/rb/lua runtimes are
  not installed on this machine (their runs fail), and Solidity refuses
  `typ` records by design (no Solidity mapping — the compiler refuses, which
  is correct CuNi behavior). The test suite gates on `--only py,js,ts,c,cpp`;
  the rest is a machine limitation, not a spec failure.

## How to run

Start the server (local only, mock oracle, binds 127.0.0.1):

```bash
cd ~/workspace/awlpay
AWL_PORT=8899 AWL_TEST_MODE=1 python3 -m server.app
```

Quote for free:
```bash
curl -s -X POST http://127.0.0.1:8899/api/pay/quote \
  -H 'Content-Type: application/json' \
  -d '{"from_chain":"ethereum","from_token":"ETH","to_chain":"base","to_token":"USDC","amount_cents":10000,"tier":0}'
```

Execute (test mode): unpaid first to see the 402, then paid:
```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8899/api/pay/execute \
  -H 'Content-Type: application/json' \
  -d '{"from_chain":"ethereum","from_token":"ETH","to_chain":"base","to_token":"USDC","amount_cents":10000,"tier":0}'
# 402
curl -s -X POST http://127.0.0.1:8899/api/pay/execute \
  -H 'Content-Type: application/json' -H 'X-Test-Payment: ok' \
  -d '{"from_chain":"ethereum","from_token":"ETH","to_chain":"base","to_token":"USDC","amount_cents":10000,"tier":0}'
# 200 + attested receipt
```

Tests (no network — `MockOracle` only; CoinGecko is never touched):
```bash
cd ~/workspace/awlpay && python3 -m pytest tests/ -q
```
40 tests: 17 core (fees/oracle/router/chamber/settlement/HTTP e2e) +
23 cross-check (18 fixture replays vs the Python mirror, 1 fixture-count
guard, 3 `cuni check` subprocess gates, 1 attested-receipt round-trip +
tamper rejection).

CuNi exactness gates:
```bash
~/workspace/cuni-langs/target/debug/cuni check spec/FeeManager.cuni --only py,js,ts,c,cpp
~/workspace/cuni-langs/target/debug/cuni check spec/SettlementEngine.cuni --only py,js,ts,c,cpp
~/workspace/cuni-langs/target/debug/cuni check spec/Profile.cuni --only py,js,ts,c,cpp
```
(`--only` is the honest scope on this machine — see HONEST LIMITS above.)

## Layout

```
awlpay/
  README.md
  spec/
    FeeManager.cuni        the fee law (CuNi)
    SettlementEngine.cuni  settlement law (CuNi)
    Profile.cuni           profile law (CuNi)
    fixtures/fees.json     18 machine-verified {inputs, fee_cents, status} fixtures
  server/
    app.py                 Starlette ASGI app: /api/pay/quote, /api/pay/execute, /health, /healthz
    logging.py             structured JSON request logs (stdout)
    fees.py                FeeManager mirror (integer cents)
    oracle.py              CoinGeckoOracle + MockOracle (PriceOracle interface)
    router.py              ConverterRouter BFS (bridge/swap hops)
    chamber.py             Ed25519 attestation envelopes
    settlement.py          mock execution: fee law + signed receipt
    requirements.txt
  tests/
    test_awlpay.py         24 tests: fees/oracle/router/chamber/settlement + HTTP e2e on the ASGI app
    test_crosscheck.py     23 cross-check tests (fixtures, cuni check, receipt round-trip)
```
