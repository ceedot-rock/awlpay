import { describe, it, before, after } from 'node:test';
import assert from 'node:assert/strict';
import type { Server } from 'node:http';
import { bookFromWholeUnitCents } from '@awlpay/sdk';
import { createApp } from '../src/app.js';

const testBook = bookFromWholeUnitCents(
  { USDC: 100, SOL: 15_000, ETH: 300_000, BTC: 6_000_000 },
  'test',
);

async function json(
  base: string,
  method: string,
  path: string,
  body?: unknown,
): Promise<{ status: number; body: any }> {
  const res = await fetch(`${base}${path}`, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  return { status: res.status, body: await res.json() };
}

describe('SettlementEngine HTTP integration', () => {
  let server: Server;
  let base: string;

  before(async () => {
    const app = createApp(undefined, async () => testBook);
    server = app.server;
    await new Promise<void>((resolve) => {
      server.listen(0, '127.0.0.1', () => resolve());
    });
    const addr = server.address();
    if (!addr || typeof addr === 'string') throw new Error('no port');
    base = `http://127.0.0.1:${addr.port}`;
  });

  after(async () => {
    await new Promise<void>((resolve, reject) => {
      server.close((err) => (err ? reject(err) : resolve()));
    });
  });

  it('GET /healthz', async () => {
    const r = await json(base, 'GET', '/healthz');
    assert.equal(r.status, 200);
    assert.deepEqual(r.body, { ok: true });
  });

  it('create wallets, credit, quote, settle success, refuse insufficient', async () => {
    const a = await json(base, 'POST', '/v1/wallets', {
      owner_id: 'alice',
      chain: 'solana',
    });
    assert.equal(a.status, 200);
    assert.equal(a.body.owner_id, 'alice');
    assert.equal(a.body.balance_cents, 0);
    assert.ok(a.body.id);
    assert.ok(a.body.created_at);

    const b = await json(base, 'POST', '/v1/wallets', {
      owner_id: 'bob',
      chain: 'base',
    });
    assert.equal(b.status, 200);
    const fromId = a.body.id as string;
    const toId = b.body.id as string;

    const credit = await json(base, 'POST', `/v1/wallets/${fromId}/credit`, {
      amount_cents: 10_000,
    });
    assert.equal(credit.status, 200);
    assert.equal(credit.body.balance_cents, 10_000);
    assert.equal(credit.body.mock, true);

    const bal = await json(base, 'GET', `/v1/wallets/${fromId}/balance`);
    assert.equal(bal.status, 200);
    assert.deepEqual(bal.body, { id: fromId, balance_cents: 10_000 });

    const quote = await json(base, 'POST', '/v1/quote', {
      amount_cents: 5_000,
      tier: 'free',
    });
    assert.equal(quote.status, 200);
    assert.equal(quote.body.platform_fee_cents, 75);
    assert.equal(quote.body.trading_fee_cents, 0);
    assert.equal(quote.body.total_debit_cents, 5_075);
    assert.equal(quote.body.tier_applied, 'free');

    const settle = await json(base, 'POST', '/v1/settle', {
      from_wallet: fromId,
      to_wallet: toId,
      amount_cents: 5_000,
      tier: 'free',
    });
    assert.equal(settle.status, 200);
    assert.equal(settle.body.status, 'settled');
    assert.equal(settle.body.amount_cents, 5_000);
    assert.equal(settle.body.platform_fee_cents, 75);
    assert.equal(settle.body.from, fromId);
    assert.equal(settle.body.to, toId);
    assert.ok(settle.body.settlement_id);

    const fromBal = await json(base, 'GET', `/v1/wallets/${fromId}/balance`);
    const toBal = await json(base, 'GET', `/v1/wallets/${toId}/balance`);
    assert.equal(fromBal.body.balance_cents, 10_000 - 5_075);
    assert.equal(toBal.body.balance_cents, 5_000);

    const refuse = await json(base, 'POST', '/v1/settle', {
      from_wallet: fromId,
      to_wallet: toId,
      amount_cents: 5_000,
      tier: 'free',
    });
    assert.equal(refuse.status, 400);
    assert.equal(refuse.body.error, 'refuse');
    assert.equal(refuse.body.reason, 'insufficient_funds');

    const fromBal2 = await json(base, 'GET', `/v1/wallets/${fromId}/balance`);
    assert.equal(fromBal2.body.balance_cents, 4_925);
  });

  it('404 unknown wallet balance', async () => {
    const r = await json(base, 'GET', '/v1/wallets/does-not-exist/balance');
    assert.equal(r.status, 404);
  });

  it('refuse same wallet and missing wallets', async () => {
    const w = await json(base, 'POST', '/v1/wallets', {
      owner_id: 'solo',
      chain: 'ethereum',
    });
    const id = w.body.id as string;
    await json(base, 'POST', `/v1/wallets/${id}/credit`, { amount_cents: 1_000 });

    const same = await json(base, 'POST', '/v1/settle', {
      from_wallet: id,
      to_wallet: id,
      amount_cents: 100,
      tier: 'l33t',
    });
    assert.equal(same.status, 400);
    assert.equal(same.body.reason, 'same_wallet');

    const missing = await json(base, 'POST', '/v1/settle', {
      from_wallet: id,
      to_wallet: 'nope',
      amount_cents: 100,
    });
    assert.equal(missing.status, 400);
    assert.equal(missing.body.reason, 'wallet_not_found');
  });
});
