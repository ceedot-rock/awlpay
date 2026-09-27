import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { quoteConversion } from '../src/router.js';

describe('quoteConversion', () => {
  it('bridges SOL into USD and adds the free fee on the payer', () => {
    const q = quoteConversion('SOL', 10_000, 'USD', 'free');
    assert.equal('error' in q, false);
    if ('error' in q) return;
    assert.equal(q.usd_cents, 15_000);
    assert.equal(q.accepted_amount_minor, 15_000);
    assert.equal(q.platform_fee_cents, 175);
    assert.equal(q.path, 'usd_bridge');
    assert.ok(q.pay_debit_minor > 10_000);
  });

  it('l33t fee is zero so the payer is debited only what they sent', () => {
    const q = quoteConversion('SOL', 10_000, 'USD', 'l33t');
    assert.equal('error' in q, false);
    if ('error' in q) return;
    assert.equal(q.platform_fee_cents, 0);
    assert.equal(q.fee_pay_minor, 0);
    assert.equal(q.pay_debit_minor, 10_000);
  });

  it('refuses when the flat fee eats the payment', () => {
    const q = quoteConversion('USD', 1, 'USD', 'free');
    assert.deepEqual(q, { error: 'refuse', reason: 'fee_exceeds_value' });
  });

  it('USD identity matches the locked free formula', () => {
    const q = quoteConversion('USD', 5_000, 'USD', 'free');
    assert.equal('error' in q, false);
    if ('error' in q) return;
    assert.equal(q.accepted_amount_minor, 5_000);
    assert.equal(q.platform_fee_cents, 75);
    assert.equal(q.pay_debit_minor, 5_075);
    assert.equal(q.path, 'identity');
  });
});
