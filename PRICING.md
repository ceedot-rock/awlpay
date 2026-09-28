# awLPay Pricing — Cost Model & Fair-Use Math (2026-09-28)

Workstream 3 (PRICING + FAIR USE). Two deliverables:

1. **Measured route pricing** for `POST /api/pay/execute` — cost-plus,
   scaled to measured compute cost, replacing the flat 25¢ v1 placeholder.
2. **L33t fair-use cap with real econ math** — replacing the 100k
   placeholder with a derived number.

All money math in the implementation is integer-only (nanocents for the
route toll, cents for fees), mirroring `spec/FeeManager.cuni`.

---

## 1. Benchmarks (measured 2026-09-28 on this VM)

VM: 2 vCPU (AMD EPYC 9D64, `nproc` = 2), CPython 3.12, MockOracle
(deterministic dict lookups — no network). Representative request:
`ETH/ethereum → USDC/base`, $100.00, free tier.

| Code path | Wall time / call | n |
|---|---|---|
| `build_quote` (hasValue ×2 + BFS + 3-tier fee math) | **41.87 µs** | 2000 |
| `router.find_path` (typical 3-hop) | 29.59 µs | 2000 |
| `router.find_path` (longest: SOL/solana → USDC/polygon, 4 hops) | 78.53 µs | 2000 |
| `calculate_fees` (one tier) | 0.27 µs | 10000 |
| `execute_quote` (fee math + Ed25519 receipt sign) | **181.52 µs** | 1000 |
| └─ `sign_attestation` (Ed25519) alone | 109.19 µs | 1000 |
| **Full execute-equivalent (quote + sign)** | **223.39 µs** | — |

The Ed25519 signature is ~49% of the execute path — it is the single
biggest line item, and it is still sub-millisecond. Fee math itself
(0.27 µs) is noise.

**Modeled compute per call: 2.23 ms** = measured 223.39 µs × **10×
safety factor** (labeled assumption) to cover HTTP parsing, TLS,
thread scheduling, and GC on a shared Fly vCPU, which the bare-Python
benchmark does not include.

---

## 2. Fly compute cost per call

Fly Machines bill per second while started. Unit rates (multiple
independent sources converge; official page
`fly.io/docs/about/pricing/` confirms the preset totals):

- Shared vCPU: **$0.77 / vCPU / month**
- RAM: **$5.00 / GB / month**
- Check: shared-cpu-1x/256 MB = 0.77×1 + 5.00×0.25 = **$2.02/mo** ✓
  (matches Fly's published figure)
- Per-second rate for that machine: $2.02 ÷ 2,628,000 s = **$0.000000769/s**

Compute cost per `/execute` call:

> 0.00223 s × $0.000000769/s = **$0.000000001715 = 172 nanocents = 0.0000001715¢**

Honest finding: **compute is not the cost driver.** The execute path is
~0.2 ms of Python; even at a 10× safety factor the Fly compute cost is
a ten-millionth of a cent per call. The toll below is dominated by the
chain-verification (RPC) line, not compute — but the compute term is in
the formula with its measured value, per Corey's "scaled to compute
cost" decision, not zeroed out.

---

## 3. Per-call chain costs (production assumptions, labeled)

The v1 402 gate is test-mode only; production ports rider-x402's on-chain
USDC verification. Modeled production cost per paid `/execute` call:

| Line | Assumption | Cost / call |
|---|---|---|
| **402 payment verification** (Base): 1× `eth_getTransactionReceipt` (15 CU) + 1× `eth_getTransactionByHash` (17 CU) = **32 CU** | Alchemy pay-as-you-go **$0.45 / 1M CU** (alchemy.com/pricing; free tier is 30M CU/mo ≈ 937k calls/mo at 32 CU — the base case prices the at-scale PAYG number, not the free tier) | 32 × $0.45/1M = **$0.0000144 = 1,440,000 nc = 0.00144¢** |
| **Price-oracle lookups** on the execute path's quote leg | Server caches prices 60 s → amortized marginal cost **≈ $0** per call. Uncached upper bound (2 CoinGecko calls @ Analyst $129/500k = $0.000258/call): $0.000516 = 51,600 nc — documented, not charged | **0 nc** |
| Compute (from §2) | measured 223.39 µs × 10 | **172 nc** |

Unit cost per call = 172 + 1,440,000 + 0 = **1,440,172 nc = 0.001440172¢**.

### The route-price formula (integer math, in `server/fees.py`)

```
price_cents = max(1, ceil(unit_nc × margin_num/margin_den ÷ 10^9))
```

- Margin: **×4 (300%)** — covers benchmark-vs-Fly-silicon error,
  unmodeled overhead, and headroom before the next whole cent. Stated,
  not hidden; the 1¢ floor binds at these unit costs, so the margin
  choice barely moves the output.
- **Minimum 1¢ toll** — sub-cent tolls are refused: verifying a
  sub-cent x402 payment on Base costs more in RPC than the payment.

> 1,440,172 nc × 4 = 5,760,688 nc = 0.00576¢ → ceil → **1¢**

### New route price: **1¢** (`ROUTE_PRICE_CENTS = 1`)

Replaces the flat 25¢ v1 placeholder. It is wired into the 402
`payment_requirements` (`price_cents`, `maxAmountRequired`,
`charged_cents`, `route_price_cents`) from `fees.ROUTE_PRICE_CENTS` —
one constant, derived, not picked.

**What 1¢ means, honestly:** the toll is ~580,000× the measured compute
cost. The toll was never about compute — at 1¢ it is a spam deterrent
($10 per 1,000 calls) plus API revenue, now with a formula behind it.
If Corey wants the stronger 25¢ deterrence back, it is one constant
(`ROUTE_MIN_CENTS`) — but then it is a deterrence pick again, and
should be labeled as such.

Re-derive rule: re-run the benchmark (`/tmp/bench_awlpay.py`
prototype), update `ROUTE_COMPUTE_NC` / `ROUTE_RPC_NC` /
`ROUTE_ORACLE_NC` in `server/fees.py`, and the toll follows.

---

## 4. L33t fair-use cap — the math

Locked: L33t = **$799/mo**, 0% platform fee, "unlimited" with a
fair-use compute guard. The guard is an honest flag (`l33t_fair_use_review`,
fee stays 0) — never a charge.

### Question
At what monthly tx count does the $799 subscription stop being
profitable — i.e. modeled variable cost per tx × cap ≈ $799 × (1 −
target margin)?

### Target margin: 65% (labeled choice)
SaaS gross margins run 70–80%, but this product has real per-tx variable
costs, crypto-abuse tail risk, and single-founder support load. 65%
contribution margin leaves 35% ($279.65) to absorb variable costs at the
cap with headroom for RPC tier flips and ticket spikes. The cap is a
circuit breaker, not a target — normal L33t users will sit far below it.

### Variable cost per tx at scale (all labeled)

| Line | Value | Source / assumption |
|---|---|---|
| Fly compute | $2.02/mo ÷ cap | shared-cpu-1x/256 MB always-on (§2). At the cap: **$0.0000158/tx** |
| RPC (Base reads for 402 verify) | **$0/tx** | 32 CU/tx × cap ≈ 4.1M CU/mo « 30M free tier. Overflow note: PAYG $0.45/M → $0.0000144/tx if the free tier is ever exceeded |
| Price oracle (amortized) | **$22.29/mo ÷ cap** | CoinGecko **Analyst $129/mo ÷ 500k calls**; 60 s server cache × 2 coins = 86,400 calls/mo → $22.29/mo. At the cap: **$0.0001746/tx**. (Free Demo tier is 10k calls/mo — insufficient with margin; paid is the honest production assumption.) |
| Support burden | **$0.002/tx** | **ASSUMPTION** (stated explicitly): $8 fully-loaded per ticket × 1 ticket per 4,000 txs. Rationale: money-movement API, power-user integrator pace (~35 tickets/mo at the cap ≈ 1/day) |
| **Total** | **$0.002190/tx = 0.2190¢/tx** | support dominates ~91% |

Note: gas is **excluded** — in the x402 `exact` scheme the payer's USDC
transfer pays its own gas; the lab never fronts it. If a relayer model
is ever adopted, this line must be re-added.

### Solve

```
cap × ($24.31/cap + $0.002) = $279.65     ($24.31 = $2.02 + $22.29 fixed/mo)
cap = ($279.65 − $24.31) / $0.002 = 127,669.4…
```

### Fair-use cap: **127,669 txs/mo** (floored — conservative direction)

Verification at the cap: 127,669 × $0.002190 = **$279.65** variable
cost → margin **65.0%** ✓.

Implemented as `L33T_TX_FAIR_USE = 127_669` in `server/fees.py`,
mirrored in `spec/FeeManager.cuni` (`txs_used > 127669`), with the
18 fee fixtures' l33t cases re-straddling the new boundary
(127668 → `l33t`, 127669 → `l33t`, 127670 → `l33t_fair_use_review`).

### Sensitivity (cap moves with the assumptions — all shown)

| Changed assumption | Cap |
|---|---|
| Base case (65% margin, $8 ticket / 4,000 txs) | **127,669** |
| Support: 1 ticket / 2,000 txs | 63,834 |
| Support: 1 ticket / 8,000 txs | 255,338 |
| Support: $5 ticket / 4,000 txs | 204,271 |
| Margin 60% (budget $319.60) | 147,644 |
| Margin 70% (budget $239.70) | 107,694 |

The support assumption drives the number (~91% of cost/tx). If Corey
disagrees with $8/4,000, the table gives him the dial.

### Abuse defense (one user hot-looping)

- Measured single-thread throughput: **~4,480 tx/s** → a hot loop hits
  the cap in **~29 seconds**. The guard is a billing-layer circuit
  breaker, not a DoS defense — per-key rate limiting is the production
  TODO for that.
- **The economic defense is the route toll**: every `/execute` still
  pays 1¢ via the 402 gate. Reaching the cap costs the abuser
  127,669 × 1¢ = **$1,276.69** in tolls. Hot-loop abuse is
  self-funding for the lab.
- Conservative framing: the cap math **excludes route-toll revenue**.
  If L33t keeps paying the 1¢ toll per call (current code does), the lab
  is profitable per-tx at *any* volume and the cap is purely a
  resource/quota circuit breaker. The 127,669 number is the worst case
  (toll waived or accounted separately).

---

## 5. What changed in code

- `server/fees.py` — `route_price_cents()` cost-plus formula (integer
  nanocents) + locked constants + `ROUTE_PRICE_CENTS = 1`;
  `L33T_TX_FAIR_USE = 127_669`. `calculate_fees` fee math **unchanged**.
- `server/app.py` — `EXECUTE_PRICE_CENTS = ROUTE_PRICE_CENTS`; 402 copy
  updated (no more "25 cents flat").
- `spec/FeeManager.cuni` — fair-use threshold 100000 → 127669; new
  `routePrice()` mirror of the toll formula (5-seat exactness gate).
- `spec/fixtures/fees.json` — 18 fixtures kept; 3 l33t cases re-straddle
  127669. Fee math untouched → all 18 still replay exactly.
- `spec/fixtures/route_price.json` — **new**, 8 fixtures for the toll
  formula (locked inputs, floor, ceil, fractional margin, large).
- `tests/test_pricing.py` — **new**, 17 tests: toll formula, fixture
  replay, pro cap edges + overage→free exactness, l33t guard edges,
  free tier unchanged.
- `tests/test_awlpay.py` — **fixed a real bug**: `sys.path` pointed at
  `~/workspace/awlpay` (a *different checkout*), so the suite was
  importing that repo's `server/` instead of the code under test. Now
  derived from `__file__`. Also updated: l33t boundary asserts,
  402 `price_cents`/`charged_cents`/`route_price_cents` == 1.

## 6. Placeholders remaining (nothing hidden)

- The 402 gate is still **test-mode only** (`X-Test-Payment`, no real
  on-chain verification) — porting rider-x402's verification is a
  separate workstream.
- Settlement is still **mock**; mainnet stays hard-disabled.
- `ROUTE_ORACLE_NC = 0` assumes the 60 s price cache ships; without it
  the honest line is 51,600 nc/call (still → 1¢ after ceil, so the toll
  is unaffected, but the *cap* math's oracle line would 3× — noted in
  the sensitivity discussion).
- Support $8/ticket @ 1/4,000 txs is the load-bearing assumption —
  flagged for Corey's review via the sensitivity table.
