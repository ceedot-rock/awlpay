import { randomUUID } from 'node:crypto';
import { calculateFees, type Tier } from '@awlpay/sdk';
import type { WalletStore } from './store.js';
import type { QuoteResult, SettleRefuse, SettleRequest, SettlementReceipt } from './types.js';

export class SettlementEngine {
  constructor(private store: WalletStore) {}

  quote(
    amount_cents: number,
    tier: Tier = 'free',
    ctx?: { volume_month_usd_cents?: number; txs_month?: number },
  ): QuoteResult {
    const fees = calculateFees(amount_cents, tier, ctx);
    return {
      amount_cents,
      platform_fee_cents: fees.platform_fee_cents,
      trading_fee_cents: fees.trading_fee_cents,
      total_debit_cents: amount_cents + fees.platform_fee_cents + fees.trading_fee_cents,
      tier_applied: fees.tier_applied,
    };
  }

  settle(req: SettleRequest): SettlementReceipt | SettleRefuse {
    const { from_wallet, to_wallet, amount_cents } = req;
    const tier: Tier = req.tier ?? 'free';

    const from = this.store.getWallet(from_wallet);
    const to = this.store.getWallet(to_wallet);

    if (!from || !to) {
      return { error: 'refuse', reason: 'wallet_not_found' };
    }
    if (amount_cents <= 0) {
      return { error: 'refuse', reason: 'invalid_amount' };
    }
    if (from_wallet === to_wallet) {
      return { error: 'refuse', reason: 'same_wallet' };
    }

    const fees = calculateFees(amount_cents, tier, {
      volume_month_usd_cents: req.volume_month_usd_cents,
      txs_month: req.txs_month,
    });
    const totalDebit = amount_cents + fees.platform_fee_cents + fees.trading_fee_cents;

    if (from.balance_cents < totalDebit) {
      return { error: 'refuse', reason: 'insufficient_funds' };
    }

    this.store.applySettlement(from_wallet, to_wallet, amount_cents, fees.platform_fee_cents);

    const receipt: SettlementReceipt = {
      settlement_id: randomUUID(),
      from: from_wallet,
      to: to_wallet,
      amount_cents,
      platform_fee_cents: fees.platform_fee_cents,
      trading_fee_cents: fees.trading_fee_cents,
      tier_applied: fees.tier_applied,
      status: 'settled',
      created_at: new Date().toISOString(),
    };
    this.store.appendReceipt(receipt);
    return receipt;
  }
}
