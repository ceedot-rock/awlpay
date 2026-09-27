import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { bookFromCoinbaseBodies, spotAmountToCents } from '../src/prices.ts';

describe('Coinbase spot parsing', () => {
  it('rounds the live amount strings to cents', () => {
    assert.equal(spotAmountToCents('84794.815'), 8_479_482);
    assert.equal(spotAmountToCents('2695.345'), 269_535);
    assert.equal(spotAmountToCents('123.385'), 12_339);
    assert.equal(spotAmountToCents('1'), 100);
  });

  it('builds a book from Coinbase bodies', () => {
    const book = bookFromCoinbaseBodies(
      {
        BTC: { data: { amount: '84794.815' } },
        ETH: { data: { amount: '2695.345' } },
        SOL: { data: { amount: '123.385' } },
        USDC: { data: { amount: '1' } },
      },
      '2026-09-27T00:00:00.000Z',
    );
    assert.equal(book.source, 'coinbase_spot');
    assert.equal(book.rates.BTC.num, 8_479_482);
    assert.equal(book.rates.SOL.num, 12_339);
    assert.equal(book.rates.USD.num, 1);
  });
});
