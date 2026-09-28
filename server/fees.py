"""awLPay FeeManager math (Python mirror of the CuNi fee spec).

EXACTNESS CONTRACT: this module must produce IDENTICAL outputs to the
CuNi spec for identical inputs. Cross-check against
~/workspace/awlpay/spec/fixtures/fees.json when it lands (tier 0=free,
1=pro, 2=l33t; fee_cents integer; status strings as returned here).
INTEGER CENTS EVERYWHERE — no floats, no rounding ambiguity.

Tiers (Corey, 2026-09-27 — locked):
    0 free: 1.0% + $0.25/tx
    1 pro:  $39/mo, cap $30k volume OR 500 txs/mo; 0% under cap;
            overage falls back to free pricing
    2 l33t: $799/mo unlimited; fair-use compute guard at 100k txs/mo
"""

from __future__ import annotations

# Tier ids
FREE = 0
PRO = 1
L33T = 2

# Free pricing: 1% + 25 cents
FREE_BPS_NUM = 1          # 1/100 of the amount
FREE_FLAT_CENTS = 25

# Pro: $39/mo, $30,000 volume cap, 500 tx cap
PRO_VOLUME_CAP_CENTS = 3_000_000
PRO_TX_CAP = 500

# L33t: $799/mo, fair-use guard
L33T_TX_FAIR_USE = 100_000


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
        return amount_cents // 100 + FREE_FLAT_CENTS, "free"

    if tier == PRO:
        under_cap = (volume_used_cents + amount_cents <= PRO_VOLUME_CAP_CENTS
                     and txs_used < PRO_TX_CAP)
        if under_cap:
            return 0, "pro_under_cap"
        return amount_cents // 100 + FREE_FLAT_CENTS, "pro_overage"

    if tier == L33T:
        if txs_used > L33T_TX_FAIR_USE:
            return 0, "l33t_fair_use_review"
        return 0, "l33t"

    raise ValueError("unknown tier %r (want 0=free, 1=pro, 2=l33t)" % (tier,))
