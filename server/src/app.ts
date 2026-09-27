import { createServer, type IncomingMessage, type ServerResponse } from 'node:http';
import { isAsset, type Asset } from '@awlpay/sdk';
import { SettlementEngine } from './settlement.js';
import { WalletStore } from './store.js';
import type { Chain, Tier } from './types.js';

const CHAINS = new Set(['solana', 'base', 'ethereum']);
const TIERS = new Set(['free', 'pro', 'l33t']);

async function readJson(req: IncomingMessage): Promise<unknown> {
  const chunks: Buffer[] = [];
  for await (const chunk of req) {
    chunks.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk));
  }
  const raw = Buffer.concat(chunks).toString('utf8');
  if (!raw) return {};
  return JSON.parse(raw);
}

function send(res: ServerResponse, status: number, body: unknown): void {
  const payload = JSON.stringify(body);
  res.writeHead(status, {
    'Content-Type': 'application/json',
    'Content-Length': Buffer.byteLength(payload),
  });
  res.end(payload);
}

function parseTier(raw: string | undefined, present: boolean): Tier | { error: string; reason: string } {
  const tier = (raw ?? 'free') as Tier;
  if ((present || raw) && !TIERS.has(tier)) {
    return { error: 'refuse', reason: 'invalid_tier' };
  }
  return tier;
}

export function createApp(store = new WalletStore()) {
  const engine = new SettlementEngine(store);

  const server = createServer(async (req, res) => {
    try {
      const url = new URL(req.url ?? '/', 'http://localhost');
      const method = req.method ?? 'GET';
      const path = url.pathname;

      if (method === 'GET' && path === '/healthz') {
        return send(res, 200, { ok: true });
      }

      if (method === 'POST' && path === '/v1/wallets') {
        const body = (await readJson(req)) as {
          owner_id?: string;
          chain?: string;
          accepted_asset?: string;
        };
        if (!body.owner_id || typeof body.owner_id !== 'string') {
          return send(res, 400, { error: 'refuse', reason: 'owner_id_required' });
        }
        const chain = (body.chain ?? 'solana') as Chain;
        if (!CHAINS.has(chain)) {
          return send(res, 400, { error: 'refuse', reason: 'invalid_chain' });
        }
        const accepted = body.accepted_asset ?? 'USD';
        if (!isAsset(accepted)) {
          return send(res, 400, { error: 'refuse', reason: 'no_value' });
        }
        const wallet = store.createWallet(body.owner_id, chain, accepted);
        return send(res, 200, wallet);
      }

      const acceptedMatch = path.match(/^\/v1\/wallets\/([^/]+)\/accepted$/);
      if (method === 'POST' && acceptedMatch) {
        const id = decodeURIComponent(acceptedMatch[1]);
        const body = (await readJson(req)) as { asset?: string };
        if (!body.asset || !isAsset(body.asset)) {
          return send(res, 400, { error: 'refuse', reason: 'no_value' });
        }
        const wallet = store.setAccepted(id, body.asset);
        if (!wallet) return send(res, 404, { error: 'not_found', reason: 'wallet_not_found' });
        return send(res, 200, {
          id: wallet.id,
          accepted_asset: wallet.accepted_asset,
        });
      }

      const balanceMatch = path.match(/^\/v1\/wallets\/([^/]+)\/balance$/);
      if (method === 'GET' && balanceMatch) {
        const id = decodeURIComponent(balanceMatch[1]);
        const wallet = store.getWallet(id);
        if (!wallet) return send(res, 404, { error: 'not_found', reason: 'wallet_not_found' });
        return send(res, 200, { id: wallet.id, balance_cents: wallet.balance_cents });
      }

      const creditMatch = path.match(/^\/v1\/wallets\/([^/]+)\/credit$/);
      if (method === 'POST' && creditMatch) {
        const id = decodeURIComponent(creditMatch[1]);
        const body = (await readJson(req)) as { amount_cents?: number; asset?: string; amount_minor?: number };
        const asset: Asset = body.asset === undefined ? 'USD' : isAsset(body.asset) ? body.asset : 'USD';
        if (body.asset !== undefined && !isAsset(body.asset)) {
          return send(res, 400, { error: 'refuse', reason: 'no_value' });
        }
        const minor = body.asset ? body.amount_minor : body.amount_cents;
        if (typeof minor !== 'number' || minor <= 0) {
          return send(res, 400, { error: 'refuse', reason: 'invalid_amount' });
        }
        const wallet = store.creditAsset(id, asset, Math.floor(minor));
        if (!wallet) return send(res, 404, { error: 'not_found', reason: 'wallet_not_found' });
        return send(res, 200, {
          id: wallet.id,
          asset,
          balance_cents: wallet.balance_cents,
          balances: wallet.balances,
          mock: true,
          note: 'DEV-ONLY mock credit; not available in production',
        });
      }

      if (method === 'POST' && path === '/v1/quote') {
        const body = (await readJson(req)) as {
          amount_cents?: number;
          pay_asset?: string;
          pay_amount_minor?: number;
          accepted_asset?: string;
          tier?: string;
          volume_month_usd_cents?: number;
          txs_month?: number;
        };
        const tierParsed = parseTier(body.tier, body.tier !== undefined);
        if (typeof tierParsed !== 'string') return send(res, 400, tierParsed);
        const ctx = {
          volume_month_usd_cents: body.volume_month_usd_cents,
          txs_month: body.txs_month,
        };
        if (body.pay_asset !== undefined) {
          if (!isAsset(body.pay_asset) || !body.accepted_asset || !isAsset(body.accepted_asset)) {
            return send(res, 400, { error: 'refuse', reason: 'no_value' });
          }
          if (typeof body.pay_amount_minor !== 'number') {
            return send(res, 400, { error: 'refuse', reason: 'invalid_amount' });
          }
          const quote = engine.quoteAny(
            body.pay_asset,
            Math.floor(body.pay_amount_minor),
            body.accepted_asset,
            tierParsed,
            ctx,
          );
          if ('error' in quote) return send(res, 400, quote);
          return send(res, 200, quote);
        }
        if (typeof body.amount_cents !== 'number' || body.amount_cents < 0) {
          return send(res, 400, { error: 'refuse', reason: 'invalid_amount' });
        }
        return send(res, 200, engine.quote(Math.floor(body.amount_cents), tierParsed, ctx));
      }

      if (method === 'POST' && path === '/v1/settle') {
        const body = (await readJson(req)) as {
          from_wallet?: string;
          to_wallet?: string;
          amount_cents?: number;
          pay_asset?: string;
          pay_amount_minor?: number;
          tier?: string;
          volume_month_usd_cents?: number;
          txs_month?: number;
        };
        if (!body.from_wallet || !body.to_wallet) {
          return send(res, 400, { error: 'refuse', reason: 'missing_fields' });
        }
        const tierParsed = parseTier(body.tier, body.tier !== undefined);
        if (typeof tierParsed !== 'string') return send(res, 400, tierParsed);
        if (body.pay_asset !== undefined) {
          if (!isAsset(body.pay_asset) || typeof body.pay_amount_minor !== 'number') {
            return send(res, 400, { error: 'refuse', reason: body.pay_asset && isAsset(body.pay_asset) ? 'invalid_amount' : 'no_value' });
          }
          const result = engine.settleAny({
            from_wallet: body.from_wallet,
            to_wallet: body.to_wallet,
            pay_asset: body.pay_asset,
            pay_amount_minor: Math.floor(body.pay_amount_minor),
            tier: tierParsed,
            volume_month_usd_cents: body.volume_month_usd_cents,
            txs_month: body.txs_month,
          });
          if ('error' in result) return send(res, 400, result);
          return send(res, 200, result);
        }
        if (typeof body.amount_cents !== 'number') {
          return send(res, 400, { error: 'refuse', reason: 'missing_fields' });
        }
        const result = engine.settle({
          from_wallet: body.from_wallet,
          to_wallet: body.to_wallet,
          amount_cents: Math.floor(body.amount_cents),
          tier: tierParsed,
          volume_month_usd_cents: body.volume_month_usd_cents,
          txs_month: body.txs_month,
        });
        if ('error' in result) return send(res, 400, result);
        return send(res, 200, result);
      }

      return send(res, 404, { error: 'not_found', reason: 'route_not_found' });
    } catch (err) {
      const message = err instanceof Error ? err.message : 'internal_error';
      return send(res, 500, { error: 'internal', reason: message });
    }
  });

  return { server, store, engine };
}
