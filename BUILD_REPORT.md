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

## What works
- `POST /api/pay/quote` (free): hasValue() gate on both tokens → BFS conversion path across 5 chains (ethereum, base, polygon, arbitrum, solana; USDC/ETH/SOL) → fees quoted for all three tiers → or honest 200 `{refused: true, reason}` (`unknown_token_no_price`, `no_conversion_path`, `dust_eaten_by_fees`).
- `POST /api/pay/execute` (402-gated, **1¢ cost-plus route toll** — derived 2026-09-28 from measured compute + Base RPC, ×4 margin, 1¢ floor; see `PRICING.md`): tier caps enforced (pro: volume+amount ≤ 3,000,000¢ AND txs < 500 → 0 fee; overage → free pricing), dust refused (net ≤ 0), receipt signed with real Ed25519.
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
3. **Paid-route pricing**: RESOLVED 2026-09-28 (workstream 3) — /execute route price is now **1¢ cost-plus** (measured compute + Base RPC for x402 verification, ×4 margin, 1¢ floor; full breakdown in `PRICING.md`), replacing the 25¢ flat placeholder.
4. **Chamber custody**: where do relayer keys actually live in production?
5. **l33t fair-use threshold**: RESOLVED 2026-09-28 (workstream 3) — **127,669 txs/mo**, econ-derived: the tx count where modeled variable cost/tx ($0.002190 — Fly compute + cached CoinGecko + $0.002/tx support assumption) consumes 35% of the $799 sub (65% margin). Full math + sensitivity table in `PRICING.md`. The support assumption ($8/ticket @ 1/4,000 txs) is the load-bearing one — flagged for Corey's review.
6. **Anything leaving the lab**: nothing under his name has left the repo. Deployment (Fly or otherwise) was explicitly out of scope and not done.
