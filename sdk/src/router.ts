// Attested mock prices. Not a live oracle. USD is the bridge.
// Integer minor units. Same inputs, same quote, or refuse.
import { calculateFees, type FeeContext, type Tier } from './fees.js';

export const ASSETS = ['USD', 'USDC', 'SOL', 'ETH', 'BTC'] as const;
export type Asset = (typeof ASSETS)[number];

/** usd_cents = floor(minor * num / den) */
const RATES: Record<Asset, { num: number; den: number }> = {
  USD: { num: 1, den: 1 },
  USDC: { num: 1, den: 1 },
  // 1.0000 SOL = $150.00
  SOL: { num: 15_000, den: 10_000 },
  // 1.0000 ETH = $3,000.00
  ETH: { num: 300_000, den: 10_000 },
  // 1.000000 BTC = $60,000.00
  BTC: { num: 6_000_000, den: 1_000_000 },
};

export function isAsset(value: string): value is Asset {
  return (ASSETS as readonly string[]).includes(value);
}

export function hasValue(asset: string): asset is Asset {
  return isAsset(asset);
}

export function toUsdCents(asset: Asset, minor: number): number {
  const rate = RATES[asset];
  return Math.floor((minor * rate.num) / rate.den);
}

export function fromUsdCents(asset: Asset, usdCents: number): number {
  const rate = RATES[asset];
  return Math.floor((usdCents * rate.den) / rate.num);
}

export interface ConvertQuote {
  pay_asset: Asset;
  pay_amount_minor: number;
  accepted_asset: Asset;
  accepted_amount_minor: number;
  usd_cents: number;
  platform_fee_cents: number;
  trading_fee_cents: number;
  fee_pay_minor: number;
  pay_debit_minor: number;
  tier_applied: string;
  path: 'identity' | 'usd_bridge';
  rate_source: 'attested_mock';
}

export type ConvertRefuse = { error: 'refuse'; reason: string };

/**
 * Payer sends `pay_amount_minor` of `pay_asset`.
 * Receiver is credited the straight USD-bridge conversion into `accepted_asset`.
 * Platform fee is an extra debit in the pay asset. If the fee eats the whole
 * payment, refuse. Unknown or unpriced assets refuse.
 */
export function quoteConversion(
  payAsset: Asset,
  payAmountMinor: number,
  acceptedAsset: Asset,
  tier: Tier | string,
  ctx?: FeeContext,
): ConvertQuote | ConvertRefuse {
  if (!Number.isInteger(payAmountMinor) || payAmountMinor <= 0) {
    return { error: 'refuse', reason: 'invalid_amount' };
  }
  if (!hasValue(payAsset) || !hasValue(acceptedAsset)) {
    return { error: 'refuse', reason: 'no_value' };
  }
  const usd = toUsdCents(payAsset, payAmountMinor);
  if (usd <= 0) {
    return { error: 'refuse', reason: 'no_value' };
  }
  let fees;
  try {
    fees = calculateFees(usd, tier, ctx);
  } catch {
    return { error: 'refuse', reason: 'unknown_tier' };
  }
  if (fees.platform_fee_cents + fees.trading_fee_cents >= usd) {
    return { error: 'refuse', reason: 'fee_exceeds_value' };
  }
  const accepted = fromUsdCents(acceptedAsset, usd);
  if (accepted <= 0) {
    return { error: 'refuse', reason: 'no_path' };
  }
  const feePay =
    fees.platform_fee_cents === 0
      ? 0
      : Math.max(1, fromUsdCents(payAsset, fees.platform_fee_cents));
  return {
    pay_asset: payAsset,
    pay_amount_minor: payAmountMinor,
    accepted_asset: acceptedAsset,
    accepted_amount_minor: accepted,
    usd_cents: usd,
    platform_fee_cents: fees.platform_fee_cents,
    trading_fee_cents: fees.trading_fee_cents,
    fee_pay_minor: feePay,
    pay_debit_minor: payAmountMinor + feePay,
    tier_applied: fees.tier_applied,
    path: payAsset === acceptedAsset ? 'identity' : 'usd_bridge',
    rate_source: 'attested_mock',
  };
}
