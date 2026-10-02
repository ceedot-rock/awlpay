# AwLPay Facilitator — x402 v2 interface

AwLPay speaks the x402 v2 facilitator wire format so external resource
servers can point their 402s at us for multi-rail payment verification.

**Base URL:** `https://awlpay.fly.dev`

## The honest difference: push payments

Standard x402 facilitators (Coinbase, PayAI) work with EIP-3009 signed
authorizations: the payer signs an authorization off-chain, the
facilitator verifies it and submits it on-chain at settle time.

AwLPay is a **push-payment verifier**: the payer sends the transaction
directly on-chain, then submits the transaction hash (plus a
replay-binding signature) as proof. We verify the on-chain receipt.

| | Standard facilitator | AwLPay facilitator |
|---|---|---|
| Payer action | Signs authorization | Sends tx on-chain |
| `/verify` | Checks signature + balance | Checks tx on-chain |
| `/settle` | Submits tx, pays gas | Re-verifies, attests receipt |
| Chains | Usually 1–2 | 11 rails |

Sellers who want the standard EIP-3009 authorization flow should use a
standard facilitator. Sellers who want **multi-rail push-payment
verification** — any chain, any token with value — use us.

## Endpoints

### GET /supported

Advertise the payment kinds we verify.

**Response:**
```json
{
  "kinds": [
    {"x402Version": 2, "scheme": "exact", "network": "eip155:8453"},
    {"x402Version": 2, "scheme": "exact", "network": "tron:0"},
    {"x402Version": 2, "scheme": "exact", "network": "stellar:pubnet"}
  ],
  "extensions": [],
  "signers": {}
}
```

### POST /verify

Validate a push-payment proof against on-chain state. Moves no money
(it already moved).

**Request:**
```json
{
  "x402Version": 2,
  "paymentPayload": {
    "x402Version": 2,
    "accepted": {
      "scheme": "exact",
      "network": "eip155:8453",
      "amount": "10000",
      "payTo": "0x..."
    },
    "payload": {
      "txHash": "0xabc...",
      "payerSig": "0xdef..."
    }
  },
  "paymentRequirements": {
    "scheme": "exact",
    "network": "eip155:8453",
    "amount": "10000",
    "payTo": "0x..."
  }
}
```

**Payload shapes by rail:**
- EVM / Tron / Bitcoin: `{"txHash": "<hex>", "payerSig": "<sig>"}`
- XRPL / Stellar: `{"txHash": "<hex>"}` (binding via InvoiceID / memo)
- Solana: `{"signature": "<base58>"}`
- Lightning: `{"preimage": "<64-hex>", "paymentHash": "<64-hex>"}`

**Response (valid):**
```json
{"isValid": true, "payer": "0x..."}
```

**Response (invalid):**
```json
{
  "isValid": false,
  "invalidReason": "insufficient_funds",
  "invalidMessage": "paid 5000 < required 10000"
}
```

Standard `invalidReason` codes: `insufficient_funds`,
`invalid_network`, `invalid_payload`, `invalid_payment_requirements`,
`invalid_transaction_state` (replay), `unexpected_verify_error`.

### POST /settle

Verify a push-payment proof and attest settlement. Since the payment is
already on-chain, settlement IS verification — we return the transaction
hash as the receipt and consume the replay key so the proof can't be
re-settled.

**Request:** same shape as `/verify`.

**Response (success):**
```json
{
  "success": true,
  "transaction": "0xabc...",
  "network": "eip155:8453",
  "payer": "0x..."
}
```

**Response (failure):**
```json
{
  "success": false,
  "transaction": "",
  "network": "eip155:8453",
  "errorReason": "insufficient_funds",
  "errorMessage": "..."
}
```

### GET /.well-known/x402

Discovery manifest: facilitator name, model, endpoint URLs, and supported
kinds.

## Integration

Point your resource server's facilitator config at us:

```typescript
const facilitator = {
  url: "https://awlpay.fly.dev",
};
```

Your 402 responses list our rails in `accepts[]`; payers pay on-chain and
submit proofs; you call `/verify` before serving and `/settle` after.

No API key required (v1). If abuse appears, we'll add keyed access.
