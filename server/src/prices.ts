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

type Priced = Exclude<Asset, 'USD'>;

/**
 * Median of a feed cluster.
 * With 4 or more prints, the high and low are dropped first.
 * Refuse when too few feeds answered, or the kept prints differ by more than 1%.
 */
export function consensusCents(values: number[], minSources: number): number {
  const clean = values.filter((n) => Number.isInteger(n) && n > 0).sort((a, b) => a - b);
  if (clean.length < minSources) throw new Error('refuse=price_unavailable');
  const used = clean.length >= 4 ? clean.slice(1, -1) : clean;
  const mid =
    used.length % 2 === 1
      ? used[(used.length - 1) / 2]!
      : Math.round((used[used.length / 2 - 1]! + used[used.length / 2]!) / 2);
  const spread = used[used.length - 1]! - used[0]!;
  if (spread * 10_000 > mid * 100) throw new Error('refuse=price_disagreement');
  return mid;
}

export function consensusBook(
  prints: Record<Priced, number[]>,
  asOf: string,
  fx?: RateBook['fx'],
): RateBook {
  const whole = {} as Record<Priced, number>;
  for (const asset of ['BTC', 'ETH', 'SOL'] as const) {
    whole[asset] = consensusCents(prints[asset] ?? [], 3);
  }
  whole.USDC = consensusCents(prints.USDC ?? [], 2);
  const book = bookFromWholeUnitCents(whole, 'consensus', asOf);
  book.fx = fx;
  return book;
}

async function getJson(url: string): Promise<unknown> {
  const res = await fetch(url, { headers: { Accept: 'application/json', 'User-Agent': 'awlpay' } });
  if (!res.ok) throw new Error(`http_${res.status}`);
  return res.json();
}

function push(bag: Record<Priced, number[]>, asset: Priced, amount: string | number | undefined): void {
  if (amount === undefined) return;
  try {
    bag[asset].push(spotAmountToCents(String(amount)));
  } catch {
    /* one bad print does not sink the book */
  }
}

async function coinbasePrints(bag: Record<Priced, number[]>): Promise<void> {
  await Promise.all(
    (Object.keys(PAIRS) as Priced[]).map(async (asset) => {
      const body = (await getJson(`https://api.coinbase.com/v2/prices/${PAIRS[asset]}/spot`)) as {
        data?: { amount?: string };
      };
      push(bag, asset, body.data?.amount);
    }),
  );
}

async function krakenPrints(bag: Record<Priced, number[]>): Promise<void> {
  const body = (await getJson(
    'https://api.kraken.com/0/public/Ticker?pair=XBTUSD,ETHUSD,SOLUSD,USDCUSD',
  )) as { result?: Record<string, { c?: string[] }> };
  const result = body.result ?? {};
  const keys = Object.keys(result);
  const pick: Record<Priced, string | undefined> = {
    BTC: keys.find((k) => k.includes('XBT') && k.endsWith('USD')),
    ETH: keys.find((k) => k.includes('XETH')),
    SOL: keys.find((k) => k.includes('SOL') && k.endsWith('USD') && !k.includes('USDC')),
    USDC: keys.find((k) => k.includes('USDC')),
  };
  for (const asset of Object.keys(pick) as Priced[]) {
    const key = pick[asset];
    push(bag, asset, key ? result[key]?.c?.[0] : undefined);
  }
}

async function coinGeckoPrints(bag: Record<Priced, number[]>): Promise<void> {
  const body = (await getJson(
    'https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum,solana,usd-coin&vs_currencies=usd',
  )) as Record<string, { usd?: number }>;
  push(bag, 'BTC', body.bitcoin?.usd);
  push(bag, 'ETH', body.ethereum?.usd);
  push(bag, 'SOL', body.solana?.usd);
  push(bag, 'USDC', body['usd-coin']?.usd);
}

async function bitstampPrints(bag: Record<Priced, number[]>): Promise<void> {
  const pairs: [Priced, string][] = [
    ['BTC', 'btcusd'],
    ['ETH', 'ethusd'],
    ['SOL', 'solusd'],
    ['USDC', 'usdcusd'],
  ];
  await Promise.all(
    pairs.map(async ([asset, pair]) => {
      const body = (await getJson(`https://www.bitstamp.net/api/v2/ticker/${pair}/`)) as { last?: string };
      push(bag, asset, body.last);
    }),
  );
}

async function geminiPrints(bag: Record<Priced, number[]>): Promise<void> {
  const pairs: [Priced, string][] = [
    ['BTC', 'btcusd'],
    ['ETH', 'ethusd'],
    ['SOL', 'solusd'],
    ['USDC', 'usdcusd'],
  ];
  await Promise.all(
    pairs.map(async ([asset, pair]) => {
      const body = (await getJson(`https://api.gemini.com/v1/pubticker/${pair}`)) as { last?: string };
      push(bag, asset, body.last);
    }),
  );
}

async function frankfurterFx(): Promise<RateBook['fx'] | undefined> {
  const body = (await getJson('https://api.frankfurter.app/latest?from=USD&to=EUR,GBP,JPY')) as {
    date?: string;
    rates?: Record<string, number>;
  };
  if (!body.date || !body.rates) return undefined;
  return { date: body.date, per_usd: body.rates };
}

function emptyBag(): Record<Priced, number[]> {
  return { BTC: [], ETH: [], SOL: [], USDC: [] };
}

/** Median of Coinbase, Kraken, CoinGecko, Bitstamp, and Gemini. Frankfurter rides along as fiat. */
export async function liveConsensusBook(now = Date.now()): Promise<RateBook> {
  if (cached && now - cached.at < TTL_MS && cached.book.source === 'consensus') return cached.book;
  const bag = emptyBag();
  const jobs = [coinbasePrints(bag), krakenPrints(bag), coinGeckoPrints(bag), bitstampPrints(bag), geminiPrints(bag)];
  await Promise.all(jobs.map((job) => job.catch(() => undefined)));
  const fx = await frankfurterFx().catch(() => undefined);
  const book = consensusBook(bag, new Date(now).toISOString(), fx);
  cached = { book, at: now };
  return book;
}
