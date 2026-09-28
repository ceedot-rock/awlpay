# awLPay v1 — BUILD REPORT (2026-09-27)

Built by coordinator + 3 workstreams. Local only. No commits, no deploys, no spending, no mainnet.

## Verified numbers
- **pytest: 40 passed, 0 failed** (`cd ~/workspace/awlpay && python3 -m pytest tests/ -q`)
  - 17 service tests (fees, oracle law, router paths/refusals, chamber tamper cases, settlement dust/mainnet guard, HTTP e2e incl. 402 → paid → 409 replay)
  - 18 parametrized fixture replays: every `spec/fixtures/fees.json` case through `server/fees.py` → **zero mismatches** vs the CuNi spec math
  - 1 fixture-count guard, 3 `cuni check` subprocess gates, 1 receipt sign/verify/tamper round-trip
- **CuNi specs: `exactness: PASS (5 langs)`** on all three (`--only py,js,ts,c,cpp`):
  - `spec/FeeManager.cuni`, `spec/SettlementEngine.cuni`, `spec/Profile.cuni`
- **Live smoke test**: quote SOL→ETH returned a 4-hop path (swap→bridge→swap) with all three tiers' fees; free tier on $1000.00 → fee 1025¢ ($10.00 + $0.25) ✓; unpaid execute → 402; test-paid execute → signed Ed25519 receipt; idempotency replay → 409.

---

## Workstream 2 — real HTTP 402 verification (2026-09-28, branch `ws-402`)

Replaced the test-mode 402 gate with real x402 payment verification, ported
from the proven `~/workspace/rider-x402` logic (no crypto reimplemented).

### Verified numbers
- **pytest: 64 passed, 0 failed, 3 consecutive runs**
  (`cd ~/workspace/awlpay-wt-402 && .venv/bin/python -m pytest tests/ -q`)
  - 17 core service tests (green; adapted from `AWL_TEST_MODE` to the new gate)
  - 23 cross-check tests (untouched, green)
  - **24 new** `tests/test_x402.py`: unpaid → 402 with v2 machine-readable
    terms; 7 malformed/tampered `X-PAYMENT` cases → 402 with a reason;
    unknown txHash → 402; wrong-signer and bit-flipped `payerSig` → 402;
    underpayment → 402; `txHash`-scheme compat → 200; payment-hash replay →
    409; idempotency-key replay → 409 (fresh payment NOT burned);
    refused-quote/malformed-body does NOT burn the payment; valid mocked
    proof → 200 + `X-PAYMENT-RESPONSE` + Ed25519 receipt that verifies
    (and fails verification when tampered); Solana balance-delta summation
    and rail-not-enabled parsing; **live read-only Base Sepolia check**;
    local-dev bypass off by default / `AWL_TEST_MODE` inert / bypass works
    with `AWL_LOCAL_DEV=1`.
- **Bugs found and fixed during the workstream:**
  - `x402.mark_payment_used()` deadlocked on first use (nested acquisition
    of a non-reentrant `_used_lock` via `_load_used()`) → `_used_lock` is now
    an `RLock`.
  - The test-only ECDSA signer normalized `s` to low-s without flipping the
    `v` parity bit, which breaks ecrecover (key recovery is not
    malleability-invariant) → test signer now emits wallet-shaped
    signatures (negate `s`, flip `v` parity), which the verifier's low-s
    guard accepts and recovers correctly.

### What works now
- `POST /api/pay/execute`: unpaid/malformed/invalid → **402** with v2
  `paymentRequirements` (`amount` in USDC base units, `payTo`, `resource`,
  per-rail `howto`); verified payment → **200** with `payment` metadata,
  `X-PAYMENT-RESPONSE` header, and the signed Ed25519 receipt; replays →
  **409** (payment hash and idempotency key tracked separately, consumed
  atomically only after the quote validates).
- Verification is **read-only**: txHash format → receipt exists+ok →
  canonical USDC `Transfer` logs to `payTo` sum ≥ 25¢ → payer = token sender
  (not `tx.from`) → EIP-191 `payerSig` binding over
  `"awlpay payment proof\ntxHash: …\nresource: …"` (ERC-1271 smart-account
  fallback; ERC-6492 undeployed refused). Binding message domain-separates
  awLPay from rider-x402 proofs.
- Multi-rail table: Base, Base Sepolia, Polygon, Arbitrum One, Optimism
  (EVM); Solana structured separately (SPL-USDC balance deltas, no
  payerSig binding yet). Adding a rail = one table entry + its `AWL_RPC_*`
  env. Base mainnet has **no public RPC fallback** (operator must configure);
  testnet rails fall back to public endpoints.
- Replay state: in-memory by default, optional JSON persistence via
  `AWL_X402_STATE` (thread-safe via `RLock`).
- CoinGecko oracle stays live; `AWL_LOCAL_DEV=1` is the only bypass (off by
  default, loud startup warning); `AWL_TEST_MODE` is retired and inert.

### Testnet limitation (honest)
No workstream-created testnet transaction exists: no throwaway wallet here
holds testnet ETH/USDC and no non-interactive faucet was reachable, so a
payer-bound end-to-end testnet payment could not be created. The live-chain
test instead reads a real historical Base Sepolia USDC transfer
(`0x37f52d3f…1e00`, 1,000 units, status ok): the verifier walks the real
receipt and Transfer logs and stops exactly at the missing-`payerSig` step —
which no stranger's key can satisfy. **No real funds moved, ever;** all
signing tests use a throwaway test key with a fixed nonce.

### What's still stubbed
- **Settlement is still MOCK** — the receipt attests to the law that *would*
  execute; coins never move.
- Solana rail is parsed and sum-verified but not caller-bound.
- `server/ethsig.py` still carries a dead `binding_message` helper with
  rider-x402's text (the live one is `x402.binding_message`); left untouched
  as provenance, do not call it.
- stdlib `http.server`; Chamber HSM custody still TODO; CoinGecko still a
  centralized trust assumption (unchanged from v1).

### Pointing at mainnet later (checklist, not done)
1. Set `AWL_RPC_BASE` to the operator's Base RPC endpoint (no fallback by
   design) and `AWL_PAY_TO` to the real treasury address.
2. Set `AWL_RELAYER_KEY` to the production Ed25519 seed (Chamber HSM when
   ready); set `AWL_X402_STATE` to a persistent path.
3. Confirm `AWL_LOCAL_DEV` is unset and `AWL_MAINNET_ENABLED` per policy.
4. Fund a throwaway wallet with a few cents of Base USDC, pay 25¢ through
   the real flow, verify the 200 + receipt, then retire the throwaway.
5. Nothing in code needs changing — mainnet vs testnet is config
   (`AWL_RPC_*` / `AWL_USDC_*`), never a code branch.

## What works
- `POST /api/pay/quote` (free): hasValue() gate on both tokens → BFS conversion path across 5 chains (ethereum, base, polygon, arbitrum, solana; USDC/ETH/SOL) → fees quoted for all three tiers → or honest 200 `{refused: true, reason}` (`unknown_token_no_price`, `no_conversion_path`, `dust_eaten_by_fees`).
- `POST /api/pay/execute` (402-gated, 25¢ route price): tier caps enforced (pro: volume+amount ≤ 3,000,000¢ AND txs < 500 → 0 fee; overage → free pricing), dust refused (net ≤ 0), receipt signed with real Ed25519.
- FeeManager CuNi spec + Python mirror agree exactly (integer cents, floor division on non-negatives).
- Price oracle: pluggable ABC; CoinGecko live-verified working (ETH $2,697.84 at build time); MockOracle for tests.

## What's stubbed (loud comments in code + README HONEST LIMITS)
- **Settlement is MOCK** — coins never move; the receipt attests to the law that *would* execute. `AWL_MAINNET_ENABLED=1` → loud refusal.
- **/execute's 402 gate is test-mode only** — `AWL_TEST_MODE=1` + `X-Test-Payment` header; no real payment rail wired (rider-x402's on-chain USDC verification is the template to port).
- **Chamber key custody** — env var `AWL_RELAYER_KEY` or ephemeral test key; Chamber HSM integration is a marked TODO. Attestation *envelope* and Ed25519 signatures are real.
- **CoinGecko oracle is a centralized trust assumption** — interface exists for a trust-minimized swap.
- **Server is stdlib `http.server`** — needs a real ASGI server for production.
- **CuNi gate is 5 seats here** — go/rs/rb/lua runtimes not installed on this VM; Solidity refuses `typ` records by design (correct CuNi behavior, affects stock examples too).
- Python raises `ValueError` on unknown tier; CuNi returns status `"refused_unknown_tier"`. Tiers 0–2 are the full contract; divergence is documented, not hidden.

## Needs Corey's decision
1. **Oracle choice**: ship with CoinGecko (free, centralized) or invest in a trust-minimized price feed before any real use?
2. **Mainnet enablement**: stays hard-disabled until he explicitly approves; real settlement + real 402 payment verification (port from rider-x402) are the two big builds before that.
3. **Paid-route pricing**: /execute route price is 25¢ flat in v1 — does he want per-call pricing tied to actual compute/cost-plus?
4. **Chamber custody**: where do relayer keys actually live in production?
5. **l33t fair-use threshold**: 100,000 txs/mo is a placeholder — needs his number.
6. **Anything leaving the lab**: nothing under his name has left the repo. Deployment (Fly or otherwise) was explicitly out of scope and not done.
