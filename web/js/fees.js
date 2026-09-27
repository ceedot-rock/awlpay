/**
 * CuNi-locked fee math — mirrors sdk calculateFees / docs/fee-manager-spec.md
 * Exact integer cents. Do not invent numbers.
 */
export function calculateFees(amount_cents, tier, ctx = {}) {
  const trading_fee_cents = 0; // getTradingFee off-chain placeholder
  const pro_cap_volume = 3_000_000; // $30k
  const pro_cap_txs = 500;
  let platform_fee_cents;
  let tier_applied;

  if (tier === 'l33t') {
    platform_fee_cents = 0;
    tier_applied = 'l33t';
  } else if (tier === 'pro') {
    const vol = ctx.volume_month_usd_cents ?? 0;
    const txs = ctx.txs_month ?? 0;
    if (vol < pro_cap_volume && txs < pro_cap_txs) {
      platform_fee_cents = 0;
      tier_applied = 'pro';
    } else {
      platform_fee_cents = Math.floor((amount_cents * 10) / 1000) + 25;
      tier_applied = 'pro_overage';
    }
  } else {
    platform_fee_cents = Math.floor((amount_cents * 10) / 1000) + 25;
    tier_applied = 'free';
  }

  return { platform_fee_cents, trading_fee_cents, tier_applied };
}

export const TIER_COPY = {
  free: { label: 'Free', price: 'Pay as you go', fee: '1.0% + $0.25' },
  pro: { label: 'Pro', price: '$39/mo', fee: '$0 under $30k volume or 500 txs; overage uses free formula' },
  l33t: { label: 'L33t', price: '$799/mo', fee: '$0 platform fee · unlimited (fair-use)' },
};
