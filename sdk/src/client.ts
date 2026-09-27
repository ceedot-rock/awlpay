import type { Tier } from './fees.js';
import type { Asset } from './router.js';

export interface AwLPayConfig {
  apiKey: string;
  baseUrl?: string;
}

export class AwLPay {
  constructor(private cfg: AwLPayConfig) {}

  private async req(path: string, opts: RequestInit = {}) {
    const res = await fetch(`${this.cfg.baseUrl ?? 'https://api.awlpay.com'}${path}`, {
      ...opts,
      headers: {
        Authorization: `Bearer ${this.cfg.apiKey}`,
        'Content-Type': 'application/json',
        ...(opts.headers || {}),
      },
    });
    return res.json();
  }

  wallets = {
    create: (
      owner_id: string,
      chain: 'solana' | 'base' | 'ethereum' = 'solana',
      accepted_asset: Asset = 'USD',
    ) =>
      this.req('/v1/wallets', {
        method: 'POST',
        body: JSON.stringify({ owner_id, chain, accepted_asset }),
      }),
    balance: (id: string) => this.req(`/v1/wallets/${id}/balance`),
    accept: (id: string, asset: Asset) =>
      this.req(`/v1/wallets/${id}/accepted`, {
        method: 'POST',
        body: JSON.stringify({ asset }),
      }),
  };

  quote = (amount_cents: number, tier: Tier) =>
    this.req('/v1/quote', { method: 'POST', body: JSON.stringify({ amount_cents, tier }) });

  quoteAny = (pay_asset: Asset, pay_amount_minor: number, accepted_asset: Asset, tier: Tier = 'free') =>
    this.req('/v1/quote', {
      method: 'POST',
      body: JSON.stringify({ pay_asset, pay_amount_minor, accepted_asset, tier }),
    });

  settle = (from_wallet: string, to_wallet: string, amount_cents: number, tier: Tier = 'free') =>
    this.req('/v1/settle', {
      method: 'POST',
      body: JSON.stringify({ from_wallet, to_wallet, amount_cents, tier }),
    });

  settleAny = (
    from_wallet: string,
    to_wallet: string,
    pay_asset: Asset,
    pay_amount_minor: number,
    tier: Tier = 'free',
  ) =>
    this.req('/v1/settle', {
      method: 'POST',
      body: JSON.stringify({ from_wallet, to_wallet, pay_asset, pay_amount_minor, tier }),
    });
}
