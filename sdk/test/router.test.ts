import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { bookFromWholeUnitCents, quoteConversion } from '../src/router.js';

const book = bookFromWholeUnitCents(
  { USDC: 100, SOL: 15_000, ETH: 300_000, BTC: 6_000_000 },
  'test',
);

describe('quoteConversion', () => {
  it('bridges SOL into USD and adds the free fee on the payer', () => {
    const q = quoteConversion('SOL', 10_000, 'USD', 'free', book);
    assert.equal('error' in q, false);
    if ('error' in q) return;
    assert.equal(q.usd_cents, 15_000);
    assert.equal(q.accepted_amount_minor, 15_000);
    assert.equal(q.platform_fee_cents, 175);
    assert.equal(q.path, 'usd_bridge');
    assert.ok(q.pay_debit_minor > 10_000);
  });

  it('l33t fee is zero so the payer is debited only what they sent', () => {
    const q = quoteConversion('SOL', 10_000, 'USD', 'l33t', book);
    assert.equal('error' in q, false);
    if ('error' in q) return;
    assert.equal(q.platform_fee_cents, 0);
    assert.equal(q.fee_pay_minor, 0);
    assert.equal(q.pay_debit_minor, 10_000);
  });

  it('refuses when the flat fee eats the payment', () => {
    const q = quoteConversion('USD', 1, 'USD', 'free', book);
    assert.deepEqual(q, { error: 'refuse', reason: 'fee_exceeds_value' });
  });

  it('credit rail puts the card fee in the quote and does not debit the coin', () => {
    const q = quoteConversion('USD', 5_000, 'USD', 'free', book, undefined, 'credit');
    assert.equal('error' in q, false);
    if ('error' in q) return;
    assert.equal(q.route_fee_cents, 175);
    assert.equal(q.platform_fee_cents, 75);
    assert.equal(q.card_charge_cents, 5_250);
    assert.equal(q.pay_debit_minor, 0);
  });

  it('obscure rail costs more than spot', () => {
    const spot = quoteConversion('SOL', 10_000, 'USD', 'l33t', book, undefined, 'spot');
    const thin = quoteConversion('SOL', 10_000, 'USD', 'l33t', book, undefined, 'obscure');
    assert.equal('error' in spot || 'error' in thin, false);
    if ('error' in spot || 'error' in thin) return;
    assert.equal(spot.route_fee_cents, 0);
    assert.equal(thin.route_fee_cents, 275);
    assert.ok(thin.pay_debit_minor > spot.pay_debit_minor);
  });

  it('USD identity matches the locked free formula', () => {
    const q = quoteConversion('USD', 5_000, 'USD', 'free', book);
    assert.equal('error' in q, false);
    if ('error' in q) return;
    assert.equal(q.accepted_amount_minor, 5_000);
    assert.equal(q.platform_fee_cents, 75);
    assert.equal(q.pay_debit_minor, 5_075);
    assert.equal(q.path, 'identity');
  });
});
