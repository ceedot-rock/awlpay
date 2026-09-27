import { bookFromWholeUnitCents, type Asset, type RateBook } from '@awlpay/sdk';

const PAIRS: Record<Exclude<Asset, 'USD'>, string> = {
  USDC: 'USDC-USD',
  SOL: 'SOL-USD',
  ETH: 'ETH-USD',
  BTC: 'BTC-USD',
};

const TTL_MS = 30_000;

let cached: { book: RateBook; at: number } | undefined;

/** Round a Coinbase spot amount string to integer USD cents. Half up. */
export function spotAmountToCents(amount: string): number {
  if (!/^\d+(\.\d+)?$/.test(amount)) {
    throw new Error('refuse=no_value');
  }
  const [whole, frac = ''] = amount.split('.');
  const dollars = Number(whole);
  const millis = Number((frac + '000').slice(0, 3));
  const cents = dollars * 100 + Math.floor(millis / 10);
  const roundUp = millis % 10 >= 5;
  const out = cents + (roundUp ? 1 : 0);
  if (!Number.isSafeInteger(out) || out <= 0) {
    throw new Error('refuse=no_value');
  }
  return out;
}

export function bookFromCoinbaseBodies(
  bodies: Record<Exclude<Asset, 'USD'>, { data?: { amount?: string } }>,
  asOf: string,
): RateBook {
  const whole = {} as Record<Exclude<Asset, 'USD'>, number>;
  for (const asset of Object.keys(PAIRS) as Exclude<Asset, 'USD'>[]) {
    const amount = bodies[asset]?.data?.amount;
    if (typeof amount !== 'string') throw new Error(`refuse=no_value:${asset}`);
    whole[asset] = spotAmountToCents(amount);
  }
  return bookFromWholeUnitCents(whole, 'coinbase_spot', asOf);
}

async function fetchPair(pair: string): Promise<{ data?: { amount?: string } }> {
  const res = await fetch(`https://api.coinbase.com/v2/prices/${pair}/spot`, {
    headers: { Accept: 'application/json' },
  });
  if (!res.ok) throw new Error(`refuse=price_unavailable:${pair}`);
  return (await res.json()) as { data?: { amount?: string } };
}

/** Live Coinbase spot book. Cached for 30 seconds. Throws if the feed fails. */
export async function liveCoinbaseBook(now = Date.now()): Promise<RateBook> {
  if (cached && now - cached.at < TTL_MS) return cached.book;
  const entries = await Promise.all(
    (Object.keys(PAIRS) as Exclude<Asset, 'USD'>[]).map(async (asset) => {
      const body = await fetchPair(PAIRS[asset]);
      return [asset, body] as const;
    }),
  );
  const bodies = Object.fromEntries(entries) as Record<
    Exclude<Asset, 'USD'>,
    { data?: { amount?: string } }
  >;
  const book = bookFromCoinbaseBodies(bodies, new Date(now).toISOString());
  cached = { book, at: now };
  return book;
}

export function clearPriceCache(): void {
  cached = undefined;
}
