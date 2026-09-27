import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { calculateFees } from '@awlpay/sdk';

describe('calculateFees (CuNi-locked)', () => {
  it('free: 1.0% + $0.25 exact cents', () => {
    // 10000 cents = $100 → 1% = 100, +25 = 125
    const r = calculateFees(10_000, 'free');
    assert.equal(r.platform_fee_cents, 125);
    assert.equal(r.trading_fee_cents, 0);
    assert.equal(r.tier_applied, 'free');
  });

  it('free: floors fractional percent', () => {
    // 999 cents → 999*10//1000 = 9, +25 = 34
    const r = calculateFees(999, 'free');
    assert.equal(r.platform_fee_cents, 34);
    assert.equal(r.tier_applied, 'free');
  });

  it('free: small amount still pays flat 25', () => {
    const r = calculateFees(1, 'free');
    assert.equal(r.platform_fee_cents, 25);
  });

  it('pro under cap: zero fee', () => {
    const r = calculateFees(10_000, 'pro', {
      volume_month_usd_cents: 2_999_999,
      txs_month: 499,
    });
    assert.equal(r.platform_fee_cents, 0);
    assert.equal(r.tier_applied, 'pro');
  });

  it('pro under cap with default ctx (0 volume/txs)', () => {
    const r = calculateFees(50_000, 'pro');
    assert.equal(r.platform_fee_cents, 0);
    assert.equal(r.tier_applied, 'pro');
  });

  it('pro overage on volume: same as free', () => {
    const r = calculateFees(10_000, 'pro', {
      volume_month_usd_cents: 3_000_000,
      txs_month: 0,
    });
    assert.equal(r.platform_fee_cents, 125);
    assert.equal(r.tier_applied, 'pro_overage');
  });

  it('pro overage on txs: same as free', () => {
    const r = calculateFees(10_000, 'pro', {
      volume_month_usd_cents: 0,
      txs_month: 500,
    });
    assert.equal(r.platform_fee_cents, 125);
    assert.equal(r.tier_applied, 'pro_overage');
  });

  it('l33t: always zero', () => {
    const r = calculateFees(1_000_000, 'l33t', {
      volume_month_usd_cents: 99_999_999,
      txs_month: 99999,
    });
    assert.equal(r.platform_fee_cents, 0);
    assert.equal(r.trading_fee_cents, 0);
    assert.equal(r.tier_applied, 'l33t');
  });
});
