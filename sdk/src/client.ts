import type { Tier } from './fees.js';

export interface AwLPayConfig {
  /** Live default: https://awlpay.fly.dev */
  baseUrl?: string;
}

/** Tier names the SDK accepts; the wire uses the server's integer ids. */
export const TIER_IDS: Record<Tier, 0 | 1 | 2> = { free: 0, pro: 1, l33t: 2 };

export interface QuoteParams {
  fromChain: string;
  fromToken: string;
  toChain: string;
  toToken: string;
  amountCents: number;
  tier?: Tier;
  volumeUsedCents?: number;
  txsUsed?: number;
  idempotencyKey?: string;
  toAddress?: string;
}

export interface PathStep {
  chain: string;
  token: string;
  hop: 'origin' | 'bridge' | 'swap';
}

export interface TierFees {
  fee_cents: number;
  status: string;
  net_cents: number;
}

export interface QuoteOk {
  path: PathStep[];
  fees: Record<Tier, TierFees>;
  tier: Tier;
  net_cents: number;
  amount_cents: number;
}

export interface QuoteRefused {
  refused: true;
  reason: string;
  detail?: string;
  fees?: Record<Tier, TierFees>;
}

export type QuoteResponse = QuoteOk | QuoteRefused;

export interface ExecuteOk {
  ok: true;
  charged_cents: number;
  route_price_cents: number;
  attestation: { alg: string; kid: string; payload: string; sig: string };
  payment: {
    via: 'x402' | 'local-dev';
    network?: string;
    tx?: string;
    payer?: string;
    paid_units?: number;
    verified?: boolean;
  };
  [k: string]: unknown;
}

export type ExecuteResponse = ExecuteOk | QuoteRefused | { error: string };

function toWireBody(p: QuoteParams): Record<string, unknown> {
  const body: Record<string, unknown> = {
    from_chain: p.fromChain,
    from_token: p.fromToken,
    to_chain: p.toChain,
    to_token: p.toToken,
    amount_cents: p.amountCents,
    tier: TIER_IDS[p.tier ?? 'free'],
  };
  if (p.volumeUsedCents !== undefined) body.volume_used_cents = p.volumeUsedCents;
  if (p.txsUsed !== undefined) body.txs_used = p.txsUsed;
  if (p.idempotencyKey !== undefined) body.idempotency_key = p.idempotencyKey;
  if (p.toAddress !== undefined) body.to_address = p.toAddress;
  return body;
}

export class AwLPay {
  private base: string;

  constructor(private cfg: AwLPayConfig = {}) {
    this.base = cfg.baseUrl ?? 'https://awlpay.fly.dev';
  }

  private async req(path: string, opts: RequestInit = {}) {
    const res = await fetch(`${this.base}${path}`, {
      ...opts,
      headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) },
    });
    return res.json();
  }

  /** GET /health — liveness. */
  health = () => this.req('/health');

  /** GET /healthz — liveness with version (Fly check target). */
  healthz = () => this.req('/healthz');

  /** GET / — service info, endpoints, tiers. */
  info = () => this.req('/');

  /**
   * POST /api/pay/quote (FREE) — priced conversion path + all-tier fees.
   * Returns QuoteOk, or { refused: true, reason } when the law says no.
   */
  quote = (p: QuoteParams): Promise<QuoteResponse> =>
    this.req('/api/pay/quote', { method: 'POST', body: JSON.stringify(toWireBody(p)) });

  /**
   * POST /api/pay/execute (402-GATED) — same body as quote, plus the
   * x402 X-PAYMENT header proving the route-price payment. Without a valid
   * payment the server answers 402 with PaymentRequirements.
   */
  execute = (p: QuoteParams, xPayment: string): Promise<ExecuteResponse> =>
    this.req('/api/pay/execute', {
      method: 'POST',
      headers: { 'X-PAYMENT': xPayment },
      body: JSON.stringify(toWireBody(p)),
    });
}
