export type Chain = 'solana' | 'base' | 'ethereum';
export type Tier = 'free' | 'pro' | 'l33t';

export interface Wallet {
  id: string;
  owner_id: string;
  chain: Chain;
  balance_cents: number;
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

export type SettleRefuse = { error: string; reason: string };
