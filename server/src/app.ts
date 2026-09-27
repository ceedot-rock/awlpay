import { createServer, type IncomingMessage, type ServerResponse } from 'node:http';
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
        const body = (await readJson(req)) as { owner_id?: string; chain?: string };
        if (!body.owner_id || typeof body.owner_id !== 'string') {
          return send(res, 400, { error: 'refuse', reason: 'owner_id_required' });
        }
        const chain = (body.chain ?? 'solana') as Chain;
        if (!CHAINS.has(chain)) {
          return send(res, 400, { error: 'refuse', reason: 'invalid_chain' });
        }
        const wallet = store.createWallet(body.owner_id, chain);
        return send(res, 200, wallet);
      }

      const balanceMatch = path.match(/^\/v1\/wallets\/([^/]+)\/balance$/);
      if (method === 'GET' && balanceMatch) {
        const id = decodeURIComponent(balanceMatch[1]);
        const wallet = store.getWallet(id);
        if (!wallet) return send(res, 404, { error: 'not_found', reason: 'wallet_not_found' });
        return send(res, 200, { id: wallet.id, balance_cents: wallet.balance_cents });
      }

      // DEV-ONLY mock funding — documented as mock-only, not for production
      const creditMatch = path.match(/^\/v1\/wallets\/([^/]+)\/credit$/);
      if (method === 'POST' && creditMatch) {
        const id = decodeURIComponent(creditMatch[1]);
        const body = (await readJson(req)) as { amount_cents?: number };
        if (typeof body.amount_cents !== 'number' || body.amount_cents <= 0) {
          return send(res, 400, { error: 'refuse', reason: 'invalid_amount' });
        }
        const wallet = store.credit(id, Math.floor(body.amount_cents));
        if (!wallet) return send(res, 404, { error: 'not_found', reason: 'wallet_not_found' });
        return send(res, 200, {
          id: wallet.id,
          balance_cents: wallet.balance_cents,
          mock: true,
          note: 'DEV-ONLY mock credit; not available in production',
        });
      }

      if (method === 'POST' && path === '/v1/quote') {
        const body = (await readJson(req)) as {
          amount_cents?: number;
          tier?: string;
          volume_month_usd_cents?: number;
          txs_month?: number;
        };
        if (typeof body.amount_cents !== 'number' || body.amount_cents < 0) {
          return send(res, 400, { error: 'refuse', reason: 'invalid_amount' });
        }
        const tier = (body.tier ?? 'free') as Tier;
        if (!TIERS.has(tier)) {
          return send(res, 400, { error: 'refuse', reason: 'invalid_tier' });
        }
        const quote = engine.quote(Math.floor(body.amount_cents), tier, {
          volume_month_usd_cents: body.volume_month_usd_cents,
          txs_month: body.txs_month,
        });
        return send(res, 200, quote);
      }

      if (method === 'POST' && path === '/v1/settle') {
        const body = (await readJson(req)) as {
          from_wallet?: string;
          to_wallet?: string;
          amount_cents?: number;
          tier?: string;
          volume_month_usd_cents?: number;
          txs_month?: number;
        };
        if (!body.from_wallet || !body.to_wallet || typeof body.amount_cents !== 'number') {
          return send(res, 400, { error: 'refuse', reason: 'missing_fields' });
        }
        const tier = (body.tier ?? 'free') as Tier;
        if (body.tier && !TIERS.has(tier)) {
          return send(res, 400, { error: 'refuse', reason: 'invalid_tier' });
        }
        const result = engine.settle({
          from_wallet: body.from_wallet,
          to_wallet: body.to_wallet,
          amount_cents: Math.floor(body.amount_cents),
          tier,
          volume_month_usd_cents: body.volume_month_usd_cents,
          txs_month: body.txs_month,
        });
        if ('error' in result) {
          return send(res, 400, result);
        }
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
