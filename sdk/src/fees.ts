export type Tier = 'free' | 'pro' | 'l33t';

export interface FeeContext {
  volume_month_usd_cents?: number;
  txs_month?: number;
}

export interface FeeResult {
  platform_fee_cents: number;
  trading_fee_cents: number;
  tier_applied: string;
}

/** CuNi-locked fee math — exact integer cents. Do not change without CoS. */
export function calculateFees(
  amount_cents: number,
  tier: Tier,
  ctx?: FeeContext,
): FeeResult {
  const trading_fee_cents = 0; // getTradingFee off-chain placeholder
  const pro_cap_volume = 3_000_000;
  const pro_cap_txs = 500;
  let platform_fee_cents: number;
  let tier_applied: string;

  if (tier === 'l33t') {
    platform_fee_cents = 0;
    tier_applied = 'l33t';
  } else if (tier === 'pro') {
    const vol = ctx?.volume_month_usd_cents ?? 0;
    const txs = ctx?.txs_month ?? 0;
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
