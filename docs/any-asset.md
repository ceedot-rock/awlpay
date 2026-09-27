# Any-asset settle

A wallet sets `accepted_asset`. A payer sends any asset that has a live USD price. The receiver is credited that payment converted into the asset they keep. Fees are extra. They do not change what the receiver keeps unless the fees eat the payment, in which case the quote refuses.

Prices are one consensus book. For each coin, AwLPay reads Coinbase, Kraken, CoinGecko, Bitstamp, and Gemini. With four or more prints it drops the high and the low, then takes the median. Bitcoin, Ether, and Solana need at least three prints. USDC needs two. If the kept prints differ by more than 1%, the quote refuses `price_disagreement`. If too few feeds answer, it refuses `price_unavailable`. It does not fall back to a made-up book. The book is cached for 30 seconds.

Frankfurter's euro, pound, and yen rates ride along as fiat reference data. They are not averaged into the coin prices. USD is one cent.

| Asset | Minor unit |
|-------|------------|
| USD | cent |
| USDC | cent |
| SOL | 0.0001 SOL |
| ETH | 0.0001 ETH |
| BTC | 0.000001 BTC |

## Route fees

The platform tier fee stays the locked FeeManager formula. The route fee is separate and is inside the same quote.

| Rail | Route fee |
|------|-----------|
| `spot` | 0 |
| `obscure` | 1.50% + $0.50 |
| `credit` | 2.90% + $0.30 |
| `debit` | 1.50% + $0.22 |

Credit and debit do not debit a coin balance. The quote returns `card_charge_cents`, which is the USD amount to authorize. This build prices that charge. It does not capture a real card.

Refuse:

| reason | when |
|--------|------|
| `no_value` | asset is not in the book |
| `no_path` | conversion rounds to nothing |
| `fee_exceeds_value` | platform fee is at least the USD value |

`POST /v1/quote` and `POST /v1/settle` take `pay_asset` plus `pay_amount_minor` for this path. Omitting them keeps the old USD-cents invoice settle.

`POST /v1/wallets` accepts `accepted_asset`. `POST /v1/wallets/{id}/accepted` changes it. `POST /v1/wallets/{id}/credit` accepts `{ asset, amount_minor }` and stays DEV-ONLY.

npm publish and Fly stay on hold.
