# Add agent payments in 10 minutes — AwLPay integrator guide

AwLPay is a payment rail for AI agents. Your agent calls an API; the API
answers 402 (Payment Required) with a list of ways to pay; your agent pays
on any supported rail and retries with proof. No accounts, no API keys,
no credit cards.

## What you need

- An HTTP client your agent already has.
- A wallet on **one** of the supported rails (see below) with a few cents
  in it.
- 10 minutes.

## The rails (11 total)

| Rail | Network ID | Asset | Proof |
|------|-----------|-------|-------|
| Base | `eip155:8453` | USDC | txHash + payerSig |
| Base Sepolia (testnet) | `eip155:84532` | USDC | txHash + payerSig |
| Polygon | `eip155:137` | USDC | txHash + payerSig |
| Arbitrum | `eip155:42161` | USDC | txHash + payerSig |
| Optimism | `eip155:10` | USDC | txHash + payerSig |
| BNB Smart Chain | `eip155:56` | USDT (18-dec) | txHash + payerSig |
| Solana | `solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp` | USDC | signature |
| XRP Ledger | `xrpl:0` | XRP | txHash (+ InvoiceID) |
| XRP Ledger Testnet | `xrpl:1` | XRP | txHash (+ InvoiceID) |
| Tron | `tron:0` | USDT | txHash + payerSig |
| Tron Nile (testnet) | `tron:1` | USDT | txHash + payerSig |
| Stellar | `stellar:pubnet` | XLM | txHash (+ memo hash) |
| Stellar Testnet | `stellar:testnet` | XLM | txHash (+ memo hash) |
| Bitcoin | `bip122:000000000019d6689c085ae165831e934` | BTC | txHash + payerSig |
| Bitcoin Signet (testnet) | `bip122:00000008819873e925422c1ff0f99f7cc9bbb` | BTC | txHash + payerSig |
| Lightning | `lightning:0` | BTC | preimage + paymentHash |

Pick the cheapest one you already hold. A single execution costs 1 cent.

## Step 1 — Call the endpoint (get a 402)

```bash
curl -s -X POST https://awlpay.fly.dev/api/pay/execute \
  -H 'Content-Type: application/json' \
  -d '{"tier": "free", "input": "hello"}'
```

You'll get HTTP 402 with a body like:

```json
{
  "x402Version": 2,
  "error": "payment required: pay 1 cents ...",
  "accepts": [
    {
      "scheme": "exact",
      "network": "eip155:8453",
      "amount": "10000",
      "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
      "payTo": "0x...",
      "resource": "https://awlpay.fly.dev/api/pay/execute",
      "extra": {
        "paymentProof": "txHash",
        "howto": "1) transfer >= 10000 USDC base units ..."
      }
    }
  ]
}
```

## Step 2 — Pay on your rail

1. Transfer **>= amount** base units of the asset to **payTo** on the
   chosen network.
2. Sign the binding message with the paying address. This stops anyone
   front-running your payment proof. The exact text and signing method
   are in `extra.howto` — it differs per rail:
   - **EVM** (Base/Polygon/Arbitrum/Optimism/BSC): EIP-191 `personal_sign`
   - **Tron**: TIP-191 (TronWeb `signMessageV2` / TronLink)
   - **Bitcoin**: Bitcoin message signature
   - **XRPL**: no signature — set `InvoiceID` (given as `extra.invoiceId`)
   - **Stellar**: no signature — set a HASH memo (`extra.memoHash`)
   - **Lightning**: pay the BOLT11 invoice, keep the preimage
3. Follow the `howto` for your rail — it's generated from live config.

## Step 3 — Retry with proof

```bash
# Build the X-PAYMENT header: base64url(JSON({
#   "x402Version": 2, "scheme": "exact", "network": "<network>",
#   "payload": {"txHash": "<0x...>", "payerSig": "<0x...>"}
# }))
XPAYMENT=$(python3 -c "
import json, base64
p = {'x402Version': 2, 'scheme': 'exact', 'network': 'eip155:8453',
     'payload': {'txHash': '0xYOUR_TX_HASH', 'payerSig': '0xYOUR_SIG'}}
print(base64.urlsafe_b64encode(json.dumps(p).encode()).decode().rstrip('='))")

curl -s -X POST https://awlpay.fly.dev/api/pay/execute \
  -H 'Content-Type: application/json' \
  -H "X-PAYMENT: $XPAYMENT" \
  -d '{"tier": "free", "input": "hello"}'
```

HTTP 200 with a signed receipt. The receipt is Ed25519-signed — verify it
offline against our published key.

**Lightning** is different: the payload is
`{"preimage": "<64-hex>", "paymentHash": "<64-hex>"}` — the preimage your
wallet reveals when the invoice settles.

## Rules your agent should know

- **Exact amounts, integer units.** 1 cent = 10,000 base units of a
  6-decimal token (USDC/USDT), 10^16 of 18-decimal USDT on BSC, drops on
  XRPL, stroops on Stellar, sats on Bitcoin, msats on Lightning.
  Never floats.
- **One proof, one execution.** A txHash can only be used once (replay =
  409). A failed request body does NOT burn your payment — retry with a
  fixed body and the same proof.
- **Underpaid = 402.** Send >= amount or don't send at all.
- **Testnets first.** Every rail has a testnet entry. Prove your
  integration there before touching real money.

## Pointing your own 402s at us (facilitator)

If you run a resource server and want multi-rail x402 without running your
own verification, use us as your facilitator:

- `GET https://awlpay.fly.dev/supported` — the rails we verify.
- `POST https://awlpay.fly.dev/verify` — check a payment payload.
- `POST https://awlpay.fly.dev/settle` — settle and get the receipt.
- `GET https://awlpay.fly.dev/.well-known/x402` — discovery manifest.

See `docs/facilitator.md` for the full interface. Note: we're a
**push-payment** facilitator — payers send transactions directly
on-chain; we verify the receipts. This is different from standard
EIP-3009 facilitators.

## Help

Something unclear? The `extra.howto` on every 402 is the source of truth —
if this guide and a `howto` disagree, the `howto` wins (it's generated
from the live server config).
