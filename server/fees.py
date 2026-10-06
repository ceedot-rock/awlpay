"""awLPay FeeManager math (Python mirror of the CuNi fee spec).

EXACTNESS CONTRACT: this module must produce IDENTICAL outputs to the
CuNi spec for identical inputs. Cross-check against
~/workspace/awlpay/spec/fixtures/fees.json when it lands (tier 0=free,
1=pro, 2=l33t; fee_cents integer; status strings as returned here).
INTEGER CENTS EVERYWHERE — no floats, no rounding ambiguity.

Tiers (Corey, 2026-09-27 — locked; Free cut 2026-10-06):
    0 free: 0.5%/tx, no fixed fee (undercuts Phantom 0.85%, MetaMask 0.875%)
    1 pro:  $39/mo, cap $30k volume OR 500 txs/mo; 0% under cap;
            overage falls back to free pricing
    2 l33t: $799/mo unlimited; fair-use compute guard at 127,669 txs/mo
            (econ-derived 2026-09-28 — see PRICING.md; was 100k placeholder)

Route price (402 toll on POST /api/pay/execute): cost-plus in integer
nanocents, derived from the 2026-09-28 benchmark — see PRICING.md for the
full breakdown. Formula: ceil(unit_cost * (1 + margin)) to whole cents,
minimum 1c (smallest sane toll: verifying a sub-cent x402 payment on Base
costs more in RPC than the payment itself).
"""

from __future__ import annotations

# Tier ids
FREE = 0
PRO = 1
L33T = 2

# Free pricing: 0.5% flat, no fixed fee (Corey 2026-10-06)
FREE_DIVISOR = 200        # amount_cents // 200 = 0.5%

# Pro: $39/mo, $30,000 volume cap, 500 tx cap
PRO_VOLUME_CAP_CENTS = 3_000_000
PRO_TX_CAP = 500

# L33t: $799/mo, fair-use guard.
# 127,669 txs/mo is NOT a picked number: it is the tx count where modeled
# variable cost per tx ($0.002190 — Fly compute + cached CoinGecko oracle +
# $0.002/tx support assumption, $0 RPC inside the free tier) consumes
# exactly 35% of the $799 subscription, i.e. a 65% contribution margin.
# Full derivation + sensitivity table in PRICING.md.
L33T_TX_FAIR_USE = 127_669

# --------------------------------------------------------------------------
# Route price: cost-plus 402 toll for POST /api/pay/execute (integer math).
# --------------------------------------------------------------------------
# All inputs are integer NANOCENTS (1c = 1,000,000,000 nc) so the formula is
# exact and deterministic — the same integer on every machine, which is why
# it can live in the CuNi spec mirror (routePrice) alongside the fee law.
NANOCENTS_PER_CENT = 1_000_000_000

# Measured 2026-09-28 on this VM (2 vCPU): full execute path
# (build_quote + execute_quote incl. Ed25519 sign) = 223.39 us/call.
# Modeled at 10x = 2.23 ms/call to cover HTTP/TLS/thread/GC overhead on a
# shared Fly vCPU. Fly shared-cpu-1x = $0.77/vCPU + $5.00/GB per month
# (256 MB -> $2.02/mo = $0.000000769/s):
#   0.00223 s * $0.000000769/s = $0.000000001715 = 171.5 nc -> 172 nc.
ROUTE_COMPUTE_NC = 172

# Production 402 payment verification (ported from rider-x402): verify the
# USDC transfer on Base = 1x eth_getTransactionReceipt (15 CU) +
# 1x eth_getTransactionByHash (17 CU) = 32 CU/call.
# Alchemy pay-as-you-go $0.45 per 1M CU (free tier is 30M CU/mo, which
# covers ~937k calls/mo at 32 CU — the base case below prices the
# at-scale number, not the free tier):
#   32 * $0.45/1,000,000 = $0.0000144 = 1,440,000 nc.
ROUTE_RPC_NC = 1_440_000

# Price-oracle lookups on the execute path's quote leg: the server caches
# prices 60 s, so the amortized marginal cost per call rounds to 0.
# Uncached upper bound (2 CoinGecko calls @ Analyst $129/500k) = 51,600 nc;
# documented in PRICING.md, not charged here.
ROUTE_ORACLE_NC = 0

# Margin: x4 (300%). Covers benchmark-vs-Fly-silicon error, unmodeled
# overhead, and headroom before the next whole cent. The 1c floor binds at
# these unit costs, so the margin choice barely moves the output — it is
# stated, not hidden.
ROUTE_MARGIN_NUM = 4
ROUTE_MARGIN_DEN = 1

# Minimum billable toll: 1c. Sub-cent tolls are refused because verifying
# them on-chain costs more than they are worth.
ROUTE_MIN_CENTS = 1


def route_price_cents(compute_nc: int = ROUTE_COMPUTE_NC,
                      rpc_nc: int = ROUTE_RPC_NC,
                      oracle_nc: int = ROUTE_ORACLE_NC,
                      margin_num: int = ROUTE_MARGIN_NUM,
                      margin_den: int = ROUTE_MARGIN_DEN) -> int:
    """Cost-plus route price in integer cents.

    price = max(1, ceil((compute + rpc + oracle) * margin_num/margin_den)).
    All inputs are integer nanocents; margin as a num/den pair. Raises
    ValueError on negative cost inputs or a non-positive margin
    denominator — those are contract violations, not prices.
    """
    for name, v in (("compute_nc", compute_nc), ("rpc_nc", rpc_nc),
                    ("oracle_nc", oracle_nc)):
        if not isinstance(v, int):
            raise TypeError("%s must be int (nanocents)" % name)
        if v < 0:
            raise ValueError("%s must be non-negative" % name)
    if margin_den <= 0:
        raise ValueError("margin_den must be positive")
    if margin_num < 0:
        raise ValueError("margin_num must be non-negative")
    unit_nc = compute_nc + rpc_nc + oracle_nc
    # ceil(unit * num/den) with integers only
    priced_nc = (unit_nc * margin_num + margin_den - 1) // margin_den
    # ceil to whole cents
    cents = (priced_nc + NANOCENTS_PER_CENT - 1) // NANOCENTS_PER_CENT
    return max(ROUTE_MIN_CENTS, cents)


# The locked toll: route_price_cents() with the constants above == 1.
ROUTE_PRICE_CENTS = route_price_cents()


def calculate_fees(amount_cents: int, tier: int,
                   volume_used_cents: int, txs_used: int) -> tuple[int, str]:
    """Return (fee_cents, status).

    All arithmetic is integer. A negative amount is not a fee question —
    it is refused.
    """
    if not isinstance(amount_cents, int):
        raise TypeError("amount_cents must be int (cents)")
    if amount_cents < 0:
        return 0, "refused_negative_amount"

    if tier == FREE:
        return amount_cents // FREE_DIVISOR, "free"

    if tier == PRO:
        under_cap = (volume_used_cents + amount_cents <= PRO_VOLUME_CAP_CENTS
                     and txs_used < PRO_TX_CAP)
        if under_cap:
            return 0, "pro_under_cap"
        return amount_cents // FREE_DIVISOR, "pro_overage"

    if tier == L33T:
        if txs_used > L33T_TX_FAIR_USE:
            return 0, "l33t_fair_use_review"
        return 0, "l33t"

    raise ValueError("unknown tier %r (want 0=free, 1=pro, 2=l33t)" % (tier,))
