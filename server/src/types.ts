import type { Asset } from '@awlpay/sdk';

export type Chain = 'solana' | 'base' | 'ethereum';
export type Tier = 'free' | 'pro' | 'l33t';
export type { Asset };

export interface Wallet {
  id: string;
  owner_id: string;
  chain: Chain;
  accepted_asset: Asset;
  /** USD minor units. Kept in lockstep with balances.USD. */
  balance_cents: number;
  balances: Record<Asset, number>;
  created_at: string;
}

export interface SettlementReceipt {
  settlement_id: string;
  from: string;
  to: string;
  amount_cents: number;
  platform_fee_cents: number;
  trading_fee_cents: number;
  tier_applied: string;
  status: 'settled';
  created_at: string;
  route_fee_cents?: number;
  pay_asset?: Asset;
  pay_amount_minor?: number;
  accepted_asset?: Asset;
  accepted_amount_minor?: number;
  path?: 'identity' | 'usd_bridge';
  rail?: 'spot' | 'obscure' | 'credit' | 'debit';
  card_charge_cents?: number;
}

export interface QuoteResult {
  amount_cents: number;
  platform_fee_cents: number;
  trading_fee_cents: number;
  total_debit_cents: number;
  tier_applied: string;
}

export interface SettleRequest {
  from_wallet: string;
  to_wallet: string;
  amount_cents: number;
  tier?: Tier;
  volume_month_usd_cents?: number;
  txs_month?: number;
}

export interface ConvertSettleRequest {
  from_wallet: string;
  to_wallet: string;
  pay_asset: Asset;
  pay_amount_minor: number;
  rail?: 'spot' | 'obscure' | 'credit' | 'debit';
  tier?: Tier;
  volume_month_usd_cents?: number;
  txs_month?: number;
}

export type SettleRefuse = { error: string; reason: string };
