import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { calculateFees } from '../src/index.ts';

// Gold from exact/FeeManager.cuni Bank fixtures (amount 10000 unless noted)
describe('FeeManager calculateFees — SoT gold (exact/FeeManager.cuni)', () => {
  it('free → platform_fee_cents=125, tier_applied=free', () => {
    const r = calculateFees(10000, 'free');
    assert.equal(r.platform_fee_cents, 125);
    assert.equal(r.tier_applied, 'free');
    assert.equal(r.trading_fee_cents, 0);
  });

  it('pro under (vol=100000, txs=10) → 0, pro', () => {
    const r = calculateFees(10000, 'pro', {
      volume_month_usd_cents: 100000,
      txs_month: 10,
    });
    assert.equal(r.platform_fee_cents, 0);
    assert.equal(r.tier_applied, 'pro');
  });

  it('pro overage volume (vol=3000000, txs=10) → 125, pro_overage', () => {
    const r = calculateFees(10000, 'pro', {
      volume_month_usd_cents: 3000000,
      txs_month: 10,
    });
    assert.equal(r.platform_fee_cents, 125);
    assert.equal(r.tier_applied, 'pro_overage');
  });

  it('pro overage txs (vol=100000, txs=500) → 125, pro_overage', () => {
    const r = calculateFees(10000, 'pro', {
      volume_month_usd_cents: 100000,
      txs_month: 500,
    });
    assert.equal(r.platform_fee_cents, 125);
    assert.equal(r.tier_applied, 'pro_overage');
  });

  it('l33t (any amount) → 0, l33t', () => {
    const r = calculateFees(99999999, 'l33t');
    assert.equal(r.platform_fee_cents, 0);
    assert.equal(r.tier_applied, 'l33t');
  });

  it('unknown tier → refuse (throw refuse=unknown_tier)', () => {
    assert.throws(
      () => calculateFees(10000, 'enterprise'),
      (err: Error) => err.message === 'refuse=unknown_tier',
    );
  });
});
