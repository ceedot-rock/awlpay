import { randomUUID } from 'node:crypto';
import { ASSETS, type Asset } from '@awlpay/sdk';
import type { Chain, SettlementReceipt, Wallet } from './types.js';

function emptyBalances(): Record<Asset, number> {
  return { USD: 0, USDC: 0, SOL: 0, ETH: 0, BTC: 0 };
}

/** In-memory wallet + settlement store (mock / local only). */
export class WalletStore {
  private wallets = new Map<string, Wallet>();
  private settlements: SettlementReceipt[] = [];

  createWallet(owner_id: string, chain: Chain, accepted_asset: Asset = 'USD'): Wallet {
    const balances = emptyBalances();
    const wallet: Wallet = {
      id: randomUUID(),
      owner_id,
      chain,
      accepted_asset,
      balance_cents: 0,
      balances,
      created_at: new Date().toISOString(),
    };
    this.wallets.set(wallet.id, wallet);
    return wallet;
  }

  getWallet(id: string): Wallet | undefined {
    return this.wallets.get(id);
  }

  setAccepted(id: string, asset: Asset): Wallet | undefined {
    const w = this.wallets.get(id);
    if (!w) return undefined;
    w.accepted_asset = asset;
    return w;
  }

  /** DEV-ONLY mock funding in USD cents. Not for production. */
  credit(id: string, amount_cents: number): Wallet | undefined {
    return this.creditAsset(id, 'USD', amount_cents);
  }

  /** DEV-ONLY mock funding in any attested asset. */
  creditAsset(id: string, asset: Asset, minor: number): Wallet | undefined {
    const w = this.wallets.get(id);
    if (!w) return undefined;
    if (!ASSETS.includes(asset) || minor <= 0) return undefined;
    w.balances[asset] += minor;
    if (asset === 'USD') w.balance_cents = w.balances.USD;
    return w;
  }

  balanceOf(id: string, asset: Asset): number | undefined {
    const w = this.wallets.get(id);
    if (!w) return undefined;
    return w.balances[asset];
  }

  applySettlement(
    fromId: string,
    toId: string,
    amount_cents: number,
    platform_fee_cents: number,
  ): void {
    this.applyConvert(fromId, 'USD', amount_cents + platform_fee_cents, toId, 'USD', amount_cents);
  }

  applyConvert(
    fromId: string,
    payAsset: Asset,
    payDebit: number,
    toId: string,
    accepted: Asset,
    creditMinor: number,
  ): void {
    const from = this.wallets.get(fromId)!;
    const to = this.wallets.get(toId)!;
    from.balances[payAsset] -= payDebit;
    to.balances[accepted] += creditMinor;
    from.balance_cents = from.balances.USD;
    to.balance_cents = to.balances.USD;
  }

  appendReceipt(receipt: SettlementReceipt): void {
    this.settlements.push(receipt);
  }

  listSettlements(): SettlementReceipt[] {
    return [...this.settlements];
  }

  reset(): void {
    this.wallets.clear();
    this.settlements = [];
  }
}
