# AwLPay

**Live: https://awlpay.fly.dev** — the payment product with no allowlist.
Any chain, any token — if it has verifiable value, it pays.

AwLPay is the money rail for agents. Quote a conversion for free
(`POST /api/pay/quote`), execute behind a 1¢ x402 payment gate
(`POST /api/pay/execute`), and every execution carries an Ed25519-signed
receipt attesting to the exact law that ran. When the math doesn't work it
refuses with a reason — never a bad rate.

**The server is the product.** `server/` is a Python (Starlette) service that
is live in production, has moved real USDC through its toll engine, and
earned real toll revenue. Everything else in this repo is tooling around it.

## What's in this repo

| Path | What it is | Status |
|------|-----------|--------|
| `server/` | **The product.** Python ASGI server: quote, x402-gated execute, toll engine, ChainRelay settlement, Chamber attestation, PWA | **Live** at awlpay.fly.dev |
| `spec/` | CuNi law: FeeManager, SettlementEngine, Profile + machine-verified fixtures | Law |
| `tests/` | 86 Python tests (fees, oracle, router, chamber, settlement, x402, chains, cross-check) | Green |
| `sdk/` | TypeScript client SDK | Client tooling — see note below |
| `mcp/` | MCP server for agent wallets | Client tooling — see note below |
| `exact/` | CuNi specs + bank receipts (fee-tier verification corpus) | Reference |
| `brand/` | Logos, lockups, icons, OG images | Assets |
| `docs/` | Product docs pack | Reference |
| `openapi.yaml` | API description | **Stale** — see note below |
| `fly.toml`, `Dockerfile`, `deploy-fly.py`, `create-machine.py` | Production deploy | Live infra |

### Client-tooling notes (read before using)

- **`sdk/`** targets `/v1/*` routes from an earlier TypeScript prototype. The
  live server speaks `/api/pay/quote` and `/api/pay/execute`. The SDK needs a
  route remap before it works against production — the fee math and types are
  good, the endpoints are not.
- **`mcp/`** defaults to `https://api.awlpay.com`; point `AWLPAY_BASE_URL` at
  `https://awlpay.fly.dev` and use the `/api/pay/*` routes.
- **`openapi.yaml`** describes the old TypeScript server's `/v1/*` surface,
  not the live Python server. Kept for reference; a fresh spec for
  `/api/pay/*` is TODO.

## Fee tiers (locked 2026-09-27)

Free: 1.0% + $0.25/tx. Pro: $39/mo, $0 under $30k volume / 500 txs. L33t:
$799/mo unlimited. All integer cents, machine-verified against the CuNi law
in `spec/`.

---

# awLPay v1

## What AwLPay is

AwLPay is a payment product with no allowlist. It accepts any token on any chain — if the token has a verifiable price, it pays. You don't need to ask whether your chain or your coin is supported. If it has value, it works.

Here is why that matters. Paying with crypto today means navigating a maze: is this chain supported, is this token accepted, which bridge, what rate, what fee. AwLPay collapses that to a single question — does it have a verifiable price? — and handles the rest. Its converter turns anything into anything whenever a conversion path exists and the amount survives the fees.

And when the math doesn't work, it refuses the payment instead of giving you a bad rate. No silent slippage, no mystery haircut — a clean refusal beats a quietly unfair deal.

The Free tier costs 1.0% plus $0.25 per payment. Pro is $39 a month with no platform fee under the cap — $30,000 in volume or 500 transactions a month, whichever comes first. L33t is $799 a month, unlimited with a fair-use guard on compute.

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
| 2 | L33t | **$799/mo** | Unlimited, `fee = 0` (`l33t`). Fair-use compute guard at **127,669 txs/mo** (econ-derived 2026-09-28 — the tx count where modeled variable cost/tx consumes 35% of the $799 sub at a 65% margin; see `PRICING.md`): `txs_used > 127_669` → `l33t_fair_use_review` (fee still 0, flagged for review). |

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

### POST /api/pay/execute — 402-gated (real x402 payment verification)

Route price: **1¢, cost-plus** per execution (derived 2026-09-28 from
measured compute + Base RPC for x402 verification, ×4 margin, 1¢ floor —
full breakdown in `PRICING.md`; replaces the 25¢ v1 flat placeholder),
paid in **USDC on a supported rail**. Unpaid requests get **HTTP 402**
with a machine-readable v2 `paymentRequirements` body and a
`PAYMENT-REQUIRED: 1` header. The client then:

1. transfers **≥ 10,000 USDC base units** (1¢) to the rail's `payTo`,
2. signs the binding message with the **paying address** —
   EIP-191 `personal_sign` over
   `"awlpay payment proof\ntxHash: <0x…>\nresource: <exact execute URL>"`
   (this binds the proof to the caller and the endpoint, so nobody can
   front-run someone else's txHash),
3. retries with header `X-PAYMENT: base64url(JSON({x402Version: 2,
   scheme: "exact", network: "eip155:8453",
   payload: {txHash: "0x…", payerSig: "0x…"}}))`.

1. transfers **≥ 250,000 USDC base units** (25¢) to the rail's `payTo`,
2. signs the binding message with the **paying address** —
   EIP-191 `personal_sign` over
   `"awlpay payment proof\ntxHash: <0x…>\nresource: <exact execute URL>"`
   (this binds the proof to the caller and the endpoint, so nobody can
   front-run someone else's txHash),
3. retries with header `X-PAYMENT: base64url(JSON({x402Version: 2,
   scheme: "exact", network: "eip155:8453",
   payload: {txHash: "0x…", payerSig: "0x…"}}))`.

The server verifies **read-only** against the chain: txHash format → receipt
exists and succeeded → canonical USDC `Transfer` logs to `payTo` sum ≥ price
→ payer is the **token sender** (not `tx.from`, so bundler/relayer flows
work) → `payerSig` recovers to that payer (ERC-1271 smart-account fallback;
undeployed ERC-6492 accounts are refused with a specific reason). Only then
is the payment hash consumed — atomically with the idempotency key — and the
route executes.

Unpaid (402) — the body lists every rail verifiable right now:
```json
{
  "x402Version": 2,
  "error": "payment required: pay 1 cents USDC on a supported rail, then retry with X-PAYMENT",
  "accepts": [{
    "scheme": "exact", "network": "eip155:8453",
    "amount": "10000",
    "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    "payTo": "0x…",
    "resource": "https://127.0.0.1:8899/api/pay/execute",
    "description": "awLPay v1 /api/pay/execute — 1 cents native USDC on Base per execution (route price)",
    "mimeType": "application/json", "maxTimeoutSeconds": 300,
    "extra": {"paymentProof": "txHash", "howto": "1) transfer ≥ 10000 USDC base units to … 2) EIP-191 personal_sign the binding message with the paying address 3) retry with X-PAYMENT …"}
  }]
}
```
Rails: Base (`eip155:8453`), Base Sepolia (`eip155:84532`), Polygon,
Arbitrum One, Optimism, and Solana (SPL USDC, balance-delta verification —
structured separately, no payerSig binding yet). Base mainnet has **no**
public RPC fallback: the operator must set `AWL_RPC_BASE` explicitly. The
other EVM rails fall back to public endpoints so testnets verify out of the
box; Solana is advertised only when `AWL_PAY_TO_SOL` is set.

Paid (real X-PAYMENT), same body as `/quote`, 200:
```json
{
  "ok": true,
  "charged_cents": 1,
  "route_price_cents": 1,
  "payment": {"via": "x402", "network": "eip155:8453", "tx": "0x…",
              "payer": "0x…", "paid_units": 10000},
  "attestation": {
    "alg": "ed25519",
    "kid": "b521ad84a17fe68f",
    "payload": "{\"amount_cents\":10000,\"fee_cents\":125,\"from_chain\":\"ethereum\",...\"mode\":\"mock\",\"net_cents\":9875,...}",
    "sig": "3363edcccacada5b…"
  }
}
```
plus an `X-PAYMENT-RESPONSE` header acknowledging the settled payment
(`{"success": true, "transaction": "0x…"}`).

The attestation payload is canonical JSON of the receipt:
`{receipt_id, from_chain, from_token, to_chain, to_token, amount_cents,
fee_cents, net_cents, tier, status, path, mode}` — verify with
`server.chamber.verify_attestation(attestation, verify_key)` (False on any
tamper; never raises).

Replay rules (all **409**):
- reused payment hash → `{"error": "replay: payment already used"}`
- reused `idempotency_key` → `{"error": "replay: idempotency_key already used"}`
  — a nonce replay never burns a fresh payment, and a refused quote /
  malformed body never burns the payment either (consumption happens only
  after the quote validates).

Local development bypass: **off by default**. Only with `AWL_LOCAL_DEV=1`
does `X-Test-Payment: ok` skip verification (the server prints a loud
startup warning in that mode). The old `AWL_TEST_MODE` is retired and inert.

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
| `AWL_LOCAL_DEV` | `0` | `1` enables the `X-Test-Payment` bypass on /execute (loud startup warning; never on in prod) |
| `AWL_TEST_MODE` | — | **retired, inert** — does nothing since the real-402 workstream |
| `AWL_RELAYER_KEY` | (ephemeral) | 64-hex-char Ed25519 seed; unset → per-process test key |
| `AWL_MAINNET_ENABLED` | `0` | `1` → settlement refuses loudly (no mainnet in v1) |
| `AWL_MODE` | `mock-local` | reported in /health |
| `AWL_PAY_TO` | *(unset)* | EVM `payTo` address that receives the 25¢ USDC (required for any EVM rail) |
| `AWL_PAY_TO_SOL` | *(unset)* | Solana `payTo` address; Solana rail advertised only when set |
| `AWL_RPC_BASE` | *(unset)* | Base mainnet RPC — **no public fallback**; set this to take real Base payments |
| `AWL_RPC_BASE_SEPOLIA` | `https://sepolia.base.org` | Base Sepolia RPC (public fallback) |
| `AWL_RPC_POLYGON` | public fallback | Polygon RPC |
| `AWL_RPC_ARBITRUM` | public fallback | Arbitrum One RPC |
| `AWL_RPC_OPTIMISM` | public fallback | Optimism RPC |
| `AWL_RPC_SOLANA` | public fallback | Solana RPC |
| `AWL_USDC_BASE` | `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913` | canonical Base USDC |
| `AWL_USDC_BASE_SEPOLIA` | `0x036CbD53842c5426634e7929541eC2318f3dCF7e` | canonical Base Sepolia USDC |
| `AWL_USDC_POLYGON` | `0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359` | override the USDC contract per rail |
| `AWL_USDC_ARBITRUM` | `0xaf88d065e77c8cC2239327C5EDb3A432268e5831` | override the USDC contract per rail |
| `AWL_USDC_OPTIMISM` | `0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85` | override the USDC contract per rail |
| `AWL_USDC_SOL_MINT` | `EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v` | override the Solana USDC mint |
| `AWL_DEFAULT_NETWORK` | `eip155:8453` | CAIP-2 id assumed when X-PAYMENT omits `network` |
| `AWL_X402_STATE` | *(unset)* | path to a JSON file for **persistent** payment-replay state; unset → in-memory only (replays forgotten on restart) |

## HONEST LIMITS

Read this before believing anything about awLPay. Nothing here is softened.

- **Settlement modes.** `AWL_EXECUTION_MODE` (default `"mock"`): mock mode
  never moves coins — the receipt attests to the law that *would* execute
  (`mode: "mock"` on every receipt). `"dryrun"` builds, signs (throwaway or
  `AWL_SETTLER_KEY_<chain>` settler key), and *simulates* each transfer hop
  (`eth_call` / `simulateTransaction`) against the configured **testnet** —
  nothing broadcasts. `"broadcast"` additionally broadcasts, and only when
  `AWL_BROADCAST=1` **and** every chain is testnet-only **and** an explicit
  recipient is set — otherwise it raises instead of downgrading. Only
  same-chain same-token transfer hops execute; swap/bridge hops refuse the
  whole settlement atomically (`refused_unwired_hop`) — no DEX or bridge
  protocol is wired yet, and Solana SPL/USDC transfers are an explicit stub.
- **No mainnet.** `AWL_MAINNET_ENABLED=1` raises loudly, and
  `server/chains.assert_testnet()` hard-refuses mainnet chain ids
  (1, 8453, 137, 42161) and Solana mainnet-beta with **no env override** —
  a mainnet flip is a code change plus explicit per-charge approval, never
  a config flip. No real funds move, ever: testnets and throwaway keys only.
- **Testnet posture.** Base Sepolia verifies out of the box via a public RPC
  fallback; Base mainnet deliberately has **no** fallback — the operator must
  set `AWL_RPC_BASE` (and `AWL_PAY_TO`) to take real Base payments. No
  mainnet payment has been taken; `AWL_MAINNET_ENABLED=1` still refuses
  loudly. No workstream-created testnet transaction exists either: no
  throwaway wallet here holds testnet ETH/USDC and no non-interactive faucet
  was available, so the live-chain test reads a real historical Base Sepolia
  USDC transfer (it verifies the receipt/logs walk and stops exactly at the
  payer-binding step, which no stranger's key can satisfy). **No real funds
  have moved, ever.**
- **The 402 gate is real now; the local bypass is off by default.**
  `X-Test-Payment: ok` buys nothing unless `AWL_LOCAL_DEV=1` (loud startup
  warning in that mode). The old `AWL_TEST_MODE` is retired and inert.
- **Replay state is in-memory by default.** Set `AWL_X402_STATE` to a JSON
  file path for persistence across restarts; without it, a restart forgets
  consumed payment hashes (idempotency keys were already in-memory-only).
- **Chamber key custody is env-var/ephemeral.** The relayer seed comes from
  `AWL_RELAYER_KEY`; unset means a per-process ephemeral key (no trust).
  Chamber HSM custody is a marked TODO in `server/chamber.py` — production
  signing must happen inside the Chamber with key-id references only.
- **CoinGecko is a centralized trust assumption.** `hasValue()` is only as
  honest as the feed. The `PriceOracle` interface is swappable
  (`server/oracle.py`); the default offline oracle is a `MockOracle`.
- **Solana rail is parsed but not payer-bound.** SPL-USDC balance deltas are
  verified read-only; the EIP-191-style caller binding exists only on the EVM
  rails. It is also advertised only when `AWL_PAY_TO_SOL` is set.
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
cd ~/workspace/awlpay-wt-402
AWL_PORT=8899 AWL_PAY_TO=0xYourAddress python3 -m server.app
```

Quote for free:
```bash
curl -s -X POST http://127.0.0.1:8899/api/pay/quote \
  -H 'Content-Type: application/json' \
  -d '{"from_chain":"ethereum","from_token":"ETH","to_chain":"base","to_token":"USDC","amount_cents":10000,"tier":0}'
```

Execute: unpaid first to see the 402, then pay 25¢ USDC to the listed
`payTo` on your chosen rail, EIP-191 `personal_sign` the binding message
with the paying address, and retry with `X-PAYMENT`:
```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8899/api/pay/execute \
  -H 'Content-Type: application/json' \
  -d '{"from_chain":"ethereum","from_token":"ETH","to_chain":"base","to_token":"USDC","amount_cents":10000,"tier":0}'
# 402 + machine-readable terms
# ... pay, sign, then:
curl -s -X POST http://127.0.0.1:8899/api/pay/execute \
  -H 'Content-Type: application/json' \
  -H "X-PAYMENT: $(python3 -c "import base64,json;print(base64.urlsafe_b64encode(json.dumps({'x402Version':2,'scheme':'exact','network':'eip155:84532','payload':{'txHash':'0x…','payerSig':'0x…'}}).encode()).decode())")" \
  -d '{"from_chain":"ethereum","from_token":"ETH","to_chain":"base","to_token":"USDC","amount_cents":10000,"tier":0}'
# 200 + attested receipt
```

Local-dev bypass (verification skipped; server warns loudly at startup):
```bash
AWL_LOCAL_DEV=1 AWL_PORT=8899 python3 -m server.app
# then: curl -H 'X-Test-Payment: ok' … → 200 without a real payment
```

Tests (mock chain reader injected; the live testnet check is read-only):
```bash
cd ~/workspace/awlpay-wt-402 && python3 -m pytest tests/ -q
```
64 tests: 17 core service tests (fees/oracle/router/chamber/settlement/HTTP
e2e) + 23 cross-check (18 fixture replays vs the Python mirror, 1
fixture-count guard, 3 `cuni check` subprocess gates, 1 attested-receipt
round-trip + tamper rejection) + 24 x402 payment tests: unpaid 402 terms,
7 malformed-payload cases, unknown txHash, wrong-signer payerSig,
bit-flipped payerSig, underpayment, txHash-scheme compat, payment-hash
replay → 409, idempotency-key replay → 409, refused-quote-does-not-burn,
valid proof → 200 + Ed25519-signed receipt (+ tamper rejection),
Solana balance-delta summation, Solana rail-needs-payTo, live Base Sepolia
read-only check, and 3 local-dev bypass cases (off by default,
`AWL_TEST_MODE` dead, bypass on with flag).

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
    x402.py                real x402 payment verification (multi-rail; ported from rider-x402)
    ethsig.py              vendored pure-stdlib Keccak-256 + secp256k1 recovery + EIP-191 verify
    logging.py             structured JSON request logs (stdout)
    fees.py                FeeManager mirror (integer cents)
    oracle.py              CoinGeckoOracle + MockOracle (PriceOracle interface)
    router.py              ConverterRouter BFS (bridge/swap hops)
    chamber.py             Ed25519 attestation envelopes
    settlement.py          quote execution: fee law + signed receipt; modes
                           mock (default) / dryrun / broadcast (gated)
    chains.py              testnet-only network table, EVM (web3.py) + Solana
                           (solders) adapters: build/sign/simulate/broadcast;
                           mainnet chain ids hard-refused, no override
    executor.py            consumes router paths hop-by-hop; transfer hops
                           execute, swap/bridge hops refuse atomically
    requirements.txt       pynacl, web3, solders
  tests/
    test_awlpay.py         24 tests: fees/oracle/router/chamber/settlement + HTTP e2e on the ASGI app
    test_crosscheck.py     23 cross-check tests (fixtures, cuni check, receipt round-trip)
    test_x402.py           17 real-402 payment tests (1c-wired ASGI port: mock chain, replay/binding/underpaid 402s, live read-only testnet check)
    test_chains.py         22 real-settlement tests (adapters, executor, modes)
```

### Grafted client tooling (from the TypeScript branch)

```
  sdk/                     TypeScript client SDK (client, fees, router types)
                           NOTE: targets /v1/* prototype routes — needs remap
                           to /api/pay/* before use against the live server
  mcp/                     MCP server (agent wallets) — set AWLPAY_BASE_URL to
                           https://awlpay.fly.dev
  brand/                   logos, lockups, app icons, favicons, OG images
  exact/                   CuNi fee-tier specs + bank receipts (verification corpus)
  docs/                    product docs pack (README-pack, android, any-asset, ...)
  openapi.yaml             STALE: describes the old /v1/* TS surface, not live
  .github/workflows/ci.yml runs Python tests + SDK tests + tsc on every push
```
