import { randomUUID } from 'node:crypto';
import type { Chain, SettlementReceipt, Wallet } from './types.js';

/** In-memory wallet + settlement store (mock / local only). */
export class WalletStore {
  private wallets = new Map<string, Wallet>();
  private settlements: SettlementReceipt[] = [];

  createWallet(owner_id: string, chain: Chain): Wallet {
    const wallet: Wallet = {
      id: randomUUID(),
      owner_id,
      chain,
      balance_cents: 0,
      created_at: new Date().toISOString(),
    };
    this.wallets.set(wallet.id, wallet);
    return wallet;
  }

  getWallet(id: string): Wallet | undefined {
    return this.wallets.get(id);
  }

  /** DEV-ONLY mock funding. Not for production. */
  credit(id: string, amount_cents: number): Wallet | undefined {
    const w = this.wallets.get(id);
    if (!w) return undefined;
    if (amount_cents <= 0) return undefined;
    w.balance_cents += amount_cents;
    return w;
  }

  applySettlement(
    fromId: string,
    toId: string,
    amount_cents: number,
    platform_fee_cents: number,
  ): void {
    const from = this.wallets.get(fromId)!;
    const to = this.wallets.get(toId)!;
    from.balance_cents -= amount_cents + platform_fee_cents;
    to.balance_cents += amount_cents;
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
