import { describe, it, before, after } from 'node:test';
import assert from 'node:assert/strict';
import type { Server } from 'node:http';
import { createApp } from '../src/app.js';

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

describe('any-asset settle into accepted asset', () => {
  let server: Server;
  let base: string;

  before(async () => {
    const app = createApp();
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

  it('SOL payment lands as USD when that is the accepted asset', async () => {
    const payer = await json(base, 'POST', '/v1/wallets', { owner_id: 'payer', chain: 'solana' });
    const recv = await json(base, 'POST', '/v1/wallets', {
      owner_id: 'recv',
      chain: 'base',
      accepted_asset: 'USD',
    });
    assert.equal(recv.body.accepted_asset, 'USD');
    const fromId = payer.body.id as string;
    const toId = recv.body.id as string;

    const funded = await json(base, 'POST', `/v1/wallets/${fromId}/credit`, {
      asset: 'SOL',
      amount_minor: 10_000,
    });
    assert.equal(funded.body.balances.SOL, 10_000);

    const quote = await json(base, 'POST', '/v1/quote', {
      pay_asset: 'SOL',
      pay_amount_minor: 10_000,
      accepted_asset: 'USD',
      tier: 'l33t',
    });
    assert.equal(quote.status, 200);
    assert.equal(quote.body.accepted_amount_minor, 15_000);
    assert.equal(quote.body.platform_fee_cents, 0);
    assert.equal(quote.body.path, 'usd_bridge');

    const settle = await json(base, 'POST', '/v1/settle', {
      from_wallet: fromId,
      to_wallet: toId,
      pay_asset: 'SOL',
      pay_amount_minor: 10_000,
      tier: 'l33t',
    });
    assert.equal(settle.status, 200);
    assert.equal(settle.body.accepted_asset, 'USD');
    assert.equal(settle.body.accepted_amount_minor, 15_000);

    const toBal = await json(base, 'GET', `/v1/wallets/${toId}/balance`);
    assert.equal(toBal.body.balance_cents, 15_000);
    assert.equal(funded.body.mock, true);
  });

  it('refuses an unpriced asset and a fee that eats the payment', async () => {
    const a = await json(base, 'POST', '/v1/wallets', { owner_id: 'a', chain: 'ethereum' });
    const b = await json(base, 'POST', '/v1/wallets', { owner_id: 'b', chain: 'ethereum' });
    const bad = await json(base, 'POST', '/v1/quote', {
      pay_asset: 'DOGE',
      pay_amount_minor: 100,
      accepted_asset: 'USD',
    });
    assert.equal(bad.status, 400);
    assert.equal(bad.body.reason, 'no_value');

    await json(base, 'POST', `/v1/wallets/${a.body.id}/credit`, { amount_cents: 100 });
    const eaten = await json(base, 'POST', '/v1/settle', {
      from_wallet: a.body.id,
      to_wallet: b.body.id,
      pay_asset: 'USD',
      pay_amount_minor: 1,
      tier: 'free',
    });
    assert.equal(eaten.status, 400);
    assert.equal(eaten.body.reason, 'fee_exceeds_value');
  });

  it('receiver can switch the asset they keep', async () => {
    const w = await json(base, 'POST', '/v1/wallets', { owner_id: 'keep', chain: 'solana' });
    const set = await json(base, 'POST', `/v1/wallets/${w.body.id}/accepted`, { asset: 'USDC' });
    assert.equal(set.status, 200);
    assert.equal(set.body.accepted_asset, 'USDC');
  });
});
