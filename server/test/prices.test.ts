import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { bookFromCoinbaseBodies, consensusBook, consensusCents, spotAmountToCents } from '../src/prices.ts';

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

describe('consensus book', () => {
  it('drops the high and low, then takes the median', () => {
    assert.equal(consensusCents([10_000, 10_010, 10_020, 10_005, 20_000], 3), 10_010);
  });

  it('refuses when the kept prints disagree by more than 1 percent', () => {
    assert.throws(() => consensusCents([10000, 10300, 10010], 3), /price_disagreement/);
  });

  it('builds one book from several prints', () => {
    const book = consensusBook(
      {
        BTC: [8_470_000, 8_471_000, 8_469_000, 8_472_000, 9_000_000],
        ETH: [269_000, 269_100, 268_900, 269_050],
        SOL: [12_300, 12_310, 12_290, 12_305],
        USDC: [100, 100, 100],
      },
      '2026-09-27T00:00:00.000Z',
      { date: '2026-09-25', per_usd: { EUR: 0.87696 } },
    );
    assert.equal(book.source, 'consensus');
    assert.equal(book.rates.BTC.num, 8_471_000);
    assert.equal(book.fx?.date, '2026-09-25');
  });
});
