// mirror of exact/FeeManager.cuni — SoT wins
export type Tier = 'free' | 'pro' | 'l33t';
export interface AwLPayConfig { apiKey: string; baseUrl?: string }

export interface FeeResult {
  platform_fee_cents: number;
  trading_fee_cents: number;
  tier_applied: string;
}

function freeFormula(amount_cents: number): number {
  // integer cents: amount_cents * 10 // 1000 + 25
  return Math.floor(amount_cents * 10 / 1000) + 25;
}

/**
 * Thin fee mirror of exact/FeeManager.cuni. Unknown tier throws — no soft PASS.
 */
export function calculateFees(
  amount_cents: number,
  tier: Tier | string,
  ctx?: { volume_month_usd_cents?: number; txs_month?: number },
): FeeResult {
  const trading_fee_cents = 0; // getTradingFee off-chain placeholder
  const pro_cap_volume = 3_000_000;
  const pro_cap_txs = 500;

  if (tier === 'l33t') {
    return { platform_fee_cents: 0, trading_fee_cents, tier_applied: 'l33t' };
  }
  if (tier === 'pro') {
    const vol = ctx?.volume_month_usd_cents ?? 0;
    const txs = ctx?.txs_month ?? 0;
    if (vol < pro_cap_volume && txs < pro_cap_txs) {
      return { platform_fee_cents: 0, trading_fee_cents, tier_applied: 'pro' };
    }
    return {
      platform_fee_cents: freeFormula(amount_cents),
      trading_fee_cents,
      tier_applied: 'pro_overage',
    };
  }
  if (tier === 'free') {
    return {
      platform_fee_cents: freeFormula(amount_cents),
      trading_fee_cents,
      tier_applied: 'free',
    };
  }
  // refuse=unknown_tier — matches FeeManager.cuni (no soft PASS)
  throw new Error('refuse=unknown_tier');
}

export class AwLPay {
  constructor(private cfg: AwLPayConfig) {}
  private async req(path: string, opts: any = {}) {
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
    create: (owner_id: string, chain: 'solana' | 'base' | 'ethereum' = 'solana') =>
      this.req('/v1/wallets', { method: 'POST', body: JSON.stringify({ owner_id, chain }) }),
    balance: (id: string) => this.req(`/v1/wallets/${id}/balance`),
  };
  quote = (amount_cents: number, tier: Tier) =>
    this.req('/v1/quote', { method: 'POST', body: JSON.stringify({ amount_cents, tier }) });
  settle = (from_wallet: string, to_wallet: string, amount_cents: number, tier: Tier = 'free') =>
    this.req('/v1/settle', {
      method: 'POST',
      body: JSON.stringify({ from_wallet, to_wallet, amount_cents, tier }),
    });
}
