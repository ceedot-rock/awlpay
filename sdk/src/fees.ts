// mirror of exact/FeeManager.cuni — SoT wins
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

function freeFormula(amount_cents: number): number {
  // Locked fee (Corey 2026-10-06): 0.5% flat, no fixed fee.
  // Exact mirror of server/fees.py FREE_DIVISOR.
  return Math.floor(amount_cents / 200);
}

/**
 * Thin fee mirror of exact/FeeManager.cuni. Unknown tier throws — no soft PASS.
 * Signature stable for Ship goldens / Bank exactness.
 */
export function calculateFees(
  amount_cents: number,
  tier: Tier | string,
  ctx?: FeeContext,
): FeeResult {
  const trading_fee_cents = 0; // getTradingFee off-chain placeholder
  const pro_cap_volume = 3_000_000;
  const pro_cap_txs = 500;

  if (tier === 'l33t') {
    return { platform_fee_cents: 0, trading_fee_cents, tier_applied: 'l33t' };
  }
  if (tier === 'pro') {
    const vol = ctx?.volume_month_usd_cents ?? 0;
    const txs = ctx?.txs_month ?? 0;
    // Exact mirror of server/fees.py calculate_fees: under cap iff the
    // CURRENT amount fits too — (volume_used + amount) <= cap.
    if (vol + amount_cents <= pro_cap_volume && txs < pro_cap_txs) {
      return { platform_fee_cents: 0, trading_fee_cents, tier_applied: 'pro' };
    }
    return {
      platform_fee_cents: freeFormula(amount_cents),
      trading_fee_cents,
      tier_applied: 'pro_overage',
    };
  }
  if (tier === 'free') {
    return {
      platform_fee_cents: freeFormula(amount_cents),
      trading_fee_cents,
      tier_applied: 'free',
    };
  }
  // refuse=unknown_tier — matches FeeManager.cuni (no soft PASS)
  throw new Error('refuse=unknown_tier');
}
