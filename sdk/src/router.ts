// USD is the bridge. Prices come from a rate book the caller supplies.
// Production passes a Coinbase spot book. Tests pass a fixed book.
// Integer minor units. Same inputs, same quote, or refuse.
import { calculateFees, type FeeContext, type Tier } from './fees.js';

export const ASSETS = ['USD', 'USDC', 'SOL', 'ETH', 'BTC'] as const;
export type Asset = (typeof ASSETS)[number];

/** How the payer funds the quote. The route fee is not the platform tier fee. */
export const RAILS = ['spot', 'obscure', 'credit', 'debit'] as const;
export type Rail = (typeof RAILS)[number];

export function isRail(value: string): value is Rail {
  return (RAILS as readonly string[]).includes(value);
}

/**
 * Route cost in USD cents, on top of the locked platform fee.
 * spot: no extra route charge.
 * obscure: 1.50% + $0.50.
 * credit: 2.90% + $0.30.
 * debit: 1.50% + $0.22.
 * Card numbers are this schedule, not a processor contract.
 */
export function routeFeeCents(rail: Rail, usdCents: number): number {
  if (rail === 'spot') return 0;
  if (rail === 'obscure') return Math.floor((usdCents * 150) / 10_000) + 50;
  if (rail === 'credit') return Math.floor((usdCents * 290) / 10_000) + 30;
  return Math.floor((usdCents * 150) / 10_000) + 22;
}

/** How many minor units make one whole coin. */
export const MINOR_PER_WHOLE: Record<Asset, number> = {
  USD: 100,
  USDC: 100,
  SOL: 10_000,
  ETH: 10_000,
  BTC: 1_000_000,
};

export interface Rate {
  /** usd_cents = floor(minor * num / den) */
  num: number;
  den: number;
}

export interface RateBook {
  rates: Record<Asset, Rate>;
  source: 'coinbase_spot' | 'consensus' | 'test';
  as_of?: string;
  /** ECB reference rates, foreign currency per 1 USD. Not mixed into coin prices. */
  fx?: { date: string; per_usd: Record<string, number> };
}

/** Build a book from whole-unit USD cent prices. USD is always 1 cent per cent. */
export function bookFromWholeUnitCents(
  wholeUnitCents: Record<Exclude<Asset, 'USD'>, number>,
  source: RateBook['source'],
  asOf?: string,
): RateBook {
  const rates = {} as Record<Asset, Rate>;
  rates.USD = { num: 1, den: 1 };
  for (const asset of ASSETS) {
    if (asset === 'USD') continue;
    const cents = wholeUnitCents[asset];
    if (!Number.isInteger(cents) || cents <= 0) {
      throw new Error(`refuse=no_value:${asset}`);
    }
    rates[asset] = { num: cents, den: MINOR_PER_WHOLE[asset] };
  }
  return { rates, source, as_of: asOf };
}

export function isAsset(value: string): value is Asset {
  return (ASSETS as readonly string[]).includes(value);
}

export function hasValue(asset: string): asset is Asset {
  return isAsset(asset);
}

export function toUsdCents(asset: Asset, minor: number, book: RateBook): number {
  const rate = book.rates[asset];
  return Math.floor((minor * rate.num) / rate.den);
}

export function fromUsdCents(asset: Asset, usdCents: number, book: RateBook): number {
  const rate = book.rates[asset];
  return Math.floor((usdCents * rate.den) / rate.num);
}

export interface ConvertQuote {
  pay_asset: Asset;
  pay_amount_minor: number;
  accepted_asset: Asset;
  accepted_amount_minor: number;
  usd_cents: number;
  platform_fee_cents: number;
  route_fee_cents: number;
  trading_fee_cents: number;
  fee_pay_minor: number;
  pay_debit_minor: number;
  /** Set when the rail is credit or debit. USD cents to authorize. Not a capture. */
  card_charge_cents?: number;
  rail: Rail;
  tier_applied: string;
  path: 'identity' | 'usd_bridge';
  rate_source: RateBook['source'];
  as_of?: string;
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
  book: RateBook,
  ctx?: FeeContext,
  rail: Rail = 'spot',
): ConvertQuote | ConvertRefuse {
  if (!Number.isInteger(payAmountMinor) || payAmountMinor <= 0) {
    return { error: 'refuse', reason: 'invalid_amount' };
  }
  if (!hasValue(payAsset) || !hasValue(acceptedAsset)) {
    return { error: 'refuse', reason: 'no_value' };
  }
  const usd = toUsdCents(payAsset, payAmountMinor, book);
  if (usd <= 0) {
    return { error: 'refuse', reason: 'no_value' };
  }
  let fees;
  try {
    fees = calculateFees(usd, tier, ctx);
  } catch {
    return { error: 'refuse', reason: 'unknown_tier' };
  }
  const routeFee = routeFeeCents(rail, usd);
  const feeUsd = fees.platform_fee_cents + fees.trading_fee_cents + routeFee;
  if (feeUsd >= usd) {
    return { error: 'refuse', reason: 'fee_exceeds_value' };
  }
  const accepted = fromUsdCents(acceptedAsset, usd, book);
  if (accepted <= 0) {
    return { error: 'refuse', reason: 'no_path' };
  }
  const feePay = feeUsd === 0 ? 0 : Math.max(1, fromUsdCents(payAsset, feeUsd, book));
  const card = rail === 'credit' || rail === 'debit';
  return {
    pay_asset: payAsset,
    pay_amount_minor: payAmountMinor,
    accepted_asset: acceptedAsset,
    accepted_amount_minor: accepted,
    usd_cents: usd,
    platform_fee_cents: fees.platform_fee_cents,
    route_fee_cents: routeFee,
    trading_fee_cents: fees.trading_fee_cents,
    fee_pay_minor: card ? 0 : feePay,
    pay_debit_minor: card ? 0 : payAmountMinor + feePay,
    card_charge_cents: card ? usd + feeUsd : undefined,
    rail,
    tier_applied: fees.tier_applied,
    path: payAsset === acceptedAsset ? 'identity' : 'usd_bridge',
    rate_source: book.source,
    as_of: book.as_of,
  };
}
