import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { calculateFees } from '../src/index.ts';

describe('FeeManager calculateFees (CuNi-locked Free/Pro/L33t)', () => {
  it('free: amount 10000 → fee 125 (100+25)', () => {
    const r = calculateFees(10000, 'free');
    assert.equal(r.platform_fee_cents, 125);
    assert.equal(r.tier_applied, 'free');
    assert.equal(r.trading_fee_cents, 0);
  });

  it('free: amount 1 → fee 25 (floor % + flat)', () => {
    const r = calculateFees(1, 'free');
    assert.equal(r.platform_fee_cents, 25);
    assert.equal(r.tier_applied, 'free');
  });

  it('pro under cap (volume and txs) → 0, tier_applied pro', () => {
    const r = calculateFees(10000, 'pro', {
      volume_month_usd_cents: 2_999_999,
      txs_month: 499,
    });
    assert.equal(r.platform_fee_cents, 0);
    assert.equal(r.tier_applied, 'pro');
  });

  it('pro overage by volume → free formula, tier_applied pro_overage', () => {
    const r = calculateFees(10000, 'pro', {
      volume_month_usd_cents: 3_000_000,
      txs_month: 0,
    });
    assert.equal(r.platform_fee_cents, 125);
    assert.equal(r.tier_applied, 'pro_overage');
  });

  it('pro overage by txs → free formula, tier_applied pro_overage', () => {
    const r = calculateFees(10000, 'pro', {
      volume_month_usd_cents: 0,
      txs_month: 500,
    });
    assert.equal(r.platform_fee_cents, 125);
    assert.equal(r.tier_applied, 'pro_overage');
  });

  it('l33t → 0, tier_applied l33t', () => {
    const r = calculateFees(50_000_00, 'l33t', {
      volume_month_usd_cents: 99_000_000,
      txs_month: 10_000,
    });
    assert.equal(r.platform_fee_cents, 0);
    assert.equal(r.tier_applied, 'l33t');
  });
});
