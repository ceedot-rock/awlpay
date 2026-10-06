# AwLPay FeeManager (CuNi-locked)

Locked 2026-09-27 from product conversation. Fee logic must match CuNi exactness: same stdout or refuse.

## Pricing

| Tier | Price | Platform fee |
|------|-------|--------------|
| free | — | 0.5% per settlement, no fixed fee |
| pro | $39/mo | 0% under cap ($30k volume **or** 500 txs/mo); overage falls back to free formula |
| l33t | $799/mo | 0% unlimited (fair-use compute guard) |

## `calculateFees`

```
type Tier = "free" | "pro" | "l33t"

pro_cap_volume_cents = 3_000_000  # $30k
pro_cap_txs = 500

if tier == "l33t":
  platform_fee_cents = 0; tier_applied = "l33t"
elif tier == "pro":
  if volume_month_usd_cents < pro_cap_volume_cents and txs_month < pro_cap_txs:
    platform_fee_cents = 0; tier_applied = "pro"
  else:
    platform_fee_cents = amount_cents * 10 // 1000 + 25; tier_applied = "pro_overage"
else:
  platform_fee_cents = amount_cents * 10 // 1000 + 25; tier_applied = "free"
```

Trading fee is off-chain placeholder (`getTradingFee`) until SettlementEngine lands.

## Product spine (from export)

- Anything-to-anything settle iff `hasValue()` + path exists + survives fees; else refuse
- Cross-chain via CuNi; start pragmatic attested relayer, design for trust-minimized swap
- Rider = signed agent identity; Chamber-sealed tier enforcement
