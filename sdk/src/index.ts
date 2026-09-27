export type Tier = 'free' | 'pro' | 'l33t';
export interface AwLPayConfig { apiKey: string; baseUrl?: string }

export function calculateFees(amount_cents: number, tier: Tier, ctx?: {volume_month_usd_cents?: number, txs_month?: number}) {
  const trading_fee_cents = 0; // getTradingFee off-chain placeholder
  const pro_cap_volume = 3000000;
  const pro_cap_txs = 500;
  let platform_fee_cents: number;
  let tier_applied: string;
  if (tier === 'l33t') { platform_fee_cents = 0; tier_applied = 'l33t'; }
  else if (tier === 'pro') {
    const vol = ctx?.volume_month_usd_cents ?? 0;
    const txs = ctx?.txs_month ?? 0;
    if (vol < pro_cap_volume && txs < pro_cap_txs) { platform_fee_cents = 0; tier_applied = 'pro'; }
    else { platform_fee_cents = Math.floor(amount_cents * 10 / 1000) + 25; tier_applied = 'pro_overage'; }
  } else { platform_fee_cents = Math.floor(amount_cents * 10 / 1000) + 25; tier_applied = 'free'; }
  return { platform_fee_cents, trading_fee_cents, tier_applied };
}

export class AwLPay {
  constructor(private cfg: AwLPayConfig) {}
  private async req(path: string, opts: any = {}) {
    const res = await fetch(`${this.cfg.baseUrl ?? 'https://api.awlpay.com'}${path}`, {
      ...opts,
      headers: { 'Authorization': `Bearer ${this.cfg.apiKey}`, 'Content-Type': 'application/json', ...(opts.headers||{}) }
    });
    return res.json();
  }
  wallets = {
    create: (owner_id: string, chain: 'solana'|'base'|'ethereum' = 'solana') =>
      this.req('/v1/wallets', { method: 'POST', body: JSON.stringify({owner_id, chain}) }),
    balance: (id: string) => this.req(`/v1/wallets/${id}/balance`),
  };
  quote = (amount_cents: number, tier: Tier) =>
    this.req('/v1/quote', { method: 'POST', body: JSON.stringify({amount_cents, tier}) });
  settle = (from_wallet: string, to_wallet: string, amount_cents: number, tier: Tier='free') =>
    this.req('/v1/settle', { method: 'POST', body: JSON.stringify({from_wallet, to_wallet, amount_cents, tier}) });
}
