# Any-asset settle (mock rates)

A wallet sets `accepted_asset`. A payer sends any attested asset. The receiver is credited that payment converted into the asset they keep. The platform fee is an extra debit in the asset the payer sent.

Rates are an attested mock book, not a live oracle. USD is the bridge.

| Asset | Minor unit | Mock price |
|-------|------------|------------|
| USD | cent | $0.01 |
| USDC | cent | $0.01 |
| SOL | 0.0001 SOL | $150.00 per SOL |
| ETH | 0.0001 ETH | $3,000.00 per ETH |
| BTC | 0.000001 BTC | $60,000.00 per BTC |

Refuse:

| reason | when |
|--------|------|
| `no_value` | asset is not in the book |
| `no_path` | conversion rounds to nothing |
| `fee_exceeds_value` | platform fee is at least the USD value |

`POST /v1/quote` and `POST /v1/settle` take `pay_asset` plus `pay_amount_minor` for this path. Omitting them keeps the old USD-cents invoice settle.

`POST /v1/wallets` accepts `accepted_asset`. `POST /v1/wallets/{id}/accepted` changes it. `POST /v1/wallets/{id}/credit` accepts `{ asset, amount_minor }` and stays DEV-ONLY.

npm publish and Fly stay on hold.
