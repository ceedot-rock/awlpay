import { randomUUID } from 'node:crypto';
import {
  calculateFees,
  quoteConversion,
  type Asset,
  type Rail,
  type RateBook,
  type Tier,
} from '@awlpay/sdk';
import type { WalletStore } from './store.js';

function refuseReason(err: unknown): string {
  if (err instanceof Error && err.message.startsWith('refuse=')) {
    return err.message.slice('refuse='.length).split(':')[0] || 'price_unavailable';
  }
  return 'price_unavailable';
}
import type {
  ConvertSettleRequest,
  QuoteResult,
  SettleRefuse,
  SettleRequest,
  SettlementReceipt,
} from './types.js';

export class SettlementEngine {
  constructor(
    private store: WalletStore,
    private loadBook: () => Promise<RateBook>,
  ) {}

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

  async quoteAny(
    pay_asset: Asset,
    pay_amount_minor: number,
    accepted_asset: Asset,
    tier: Tier = 'free',
    ctx?: { volume_month_usd_cents?: number; txs_month?: number },
    rail: Rail = 'spot',
  ) {
    let book: RateBook;
    try {
      book = await this.loadBook();
    } catch (err) {
      return { error: 'refuse' as const, reason: refuseReason(err) };
    }
    return quoteConversion(pay_asset, pay_amount_minor, accepted_asset, tier, book, ctx, rail);
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

    if ((this.store.balanceOf(from_wallet, 'USD') ?? 0) < totalDebit) {
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
      pay_asset: 'USD',
      accepted_asset: 'USD',
      path: 'identity',
    };
    this.store.appendReceipt(receipt);
    return receipt;
  }

  /**
   * Payer sends any attested asset. Receiver is credited in their accepted asset.
   * Fee is extra, in the pay asset. No path or a fee that eats the value refuses.
   */
  async settleAny(req: ConvertSettleRequest): Promise<SettlementReceipt | SettleRefuse> {
    const from = this.store.getWallet(req.from_wallet);
    const to = this.store.getWallet(req.to_wallet);
    if (!from || !to) return { error: 'refuse', reason: 'wallet_not_found' };
    if (req.from_wallet === req.to_wallet) return { error: 'refuse', reason: 'same_wallet' };

    const tier: Tier = req.tier ?? 'free';
    let book: RateBook;
    try {
      book = await this.loadBook();
    } catch (err) {
      return { error: 'refuse', reason: refuseReason(err) };
    }
    const quoted = quoteConversion(
      req.pay_asset,
      req.pay_amount_minor,
      to.accepted_asset,
      tier,
      book,
      {
        volume_month_usd_cents: req.volume_month_usd_cents,
        txs_month: req.txs_month,
      },
      req.rail ?? 'spot',
    );
    if ('error' in quoted) return quoted;

    const card = quoted.rail === 'credit' || quoted.rail === 'debit';
    if (!card) {
      const have = this.store.balanceOf(req.from_wallet, req.pay_asset) ?? 0;
      if (have < quoted.pay_debit_minor) {
        return { error: 'refuse', reason: 'insufficient_funds' };
      }
      this.store.applyConvert(
        req.from_wallet,
        quoted.pay_asset,
        quoted.pay_debit_minor,
        req.to_wallet,
        quoted.accepted_asset,
        quoted.accepted_amount_minor,
      );
    } else {
      this.store.creditAsset(req.to_wallet, quoted.accepted_asset, quoted.accepted_amount_minor);
    }

    const receipt: SettlementReceipt = {
      settlement_id: randomUUID(),
      from: req.from_wallet,
      to: req.to_wallet,
      amount_cents: quoted.usd_cents,
      platform_fee_cents: quoted.platform_fee_cents,
      trading_fee_cents: quoted.trading_fee_cents,
      tier_applied: quoted.tier_applied,
      status: 'settled',
      created_at: new Date().toISOString(),
      pay_asset: quoted.pay_asset,
      pay_amount_minor: quoted.pay_amount_minor,
      accepted_asset: quoted.accepted_asset,
      accepted_amount_minor: quoted.accepted_amount_minor,
      path: quoted.path,
      rail: quoted.rail,
      route_fee_cents: quoted.route_fee_cents,
      card_charge_cents: quoted.card_charge_cents,
    };
    this.store.appendReceipt(receipt);
    return receipt;
  }
}
