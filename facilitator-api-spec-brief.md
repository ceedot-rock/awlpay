# x402 Facilitator API — Spec Brief for AwLPay

Research date: 2026-10-02. Sources: `x402-foundation/x402` repo (canonical home;
`coinbase/x402` is a dev fork) — `specs/x402-specification-v2.md` §7 (Facilitator
Interface), §8 (Discovery), §9 (error codes); `typescript/packages/core/src/types/facilitator.ts`
(canonical TS types); `go/FACILITATOR.md`; e2e facilitator READMEs; third-party
facilitator docs (PayAI, Stack, Dexter, OpenZeppelin relayer plugin, T54 XRPL).

## 1. Endpoints (HTTP, JSON)

Three standard endpoints. Base URL is arbitrary — sellers configure the full base
(e.g. `https://awlpay.fly.dev`, or a path prefix like Stack's
`https://stack.perkos.xyz/api/v2/x402/`).

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/supported` | Advertise supported payment kinds, extensions, signer addresses |
| `POST` | `/verify` | Validate a payment authorization **without** touching the chain |
| `POST` | `/settle` | Execute the payment on-chain and return the receipt |

Optional (reference implementations add these; not in the wire spec):
`GET /health`, `POST /close` (graceful shutdown for test harnesses),
`GET /discovery/resources` (Bazaar resource discovery, spec §8).

## 2. Request / response schemas

### POST /verify and POST /settle — request (identical shape for both)

```json
{
  "x402Version": 2,
  "paymentPayload": { /* PaymentPayload, §2.2 */ },
  "paymentRequirements": { /* PaymentRequirements, §2.1 */ }
}
```

TS types (`VerifyRequest`, `SettleRequest`):
```ts
type VerifyRequest = { x402Version: number; paymentPayload: PaymentPayload; paymentRequirements: PaymentRequirements; };
type SettleRequest  = { x402Version: number; paymentPayload: PaymentPayload; paymentRequirements: PaymentRequirements; };
```
Notes:
- `x402Version` top-level in the request body is required by all four SDKs (added to
  the spec via PR #1312; older v1 requests used `paymentHeader: "<base64 X-PAYMENT>"`
  instead of `paymentPayload` — see §6).
- The **same body** goes to both endpoints; schemes may interpret fields differently
  at settle time vs verify time (e.g. `upto` scheme: `amount` = max authorized at
  verify, actual settled amount at settle).

### §2.1 PaymentRequirements (v2)

| Field | Type | Required | Notes |
|---|---|---|---|
| `scheme` | string | yes | e.g. `"exact"` |
| `network` | string | yes | CAIP-2, e.g. `eip155:8453` (Base), `eip155:84532` (Base Sepolia), `solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp` |
| `amount` | string | yes | Atomic token units, integer as string, e.g. `"10000"` (1¢ USDC) |
| `asset` | string | yes | Token contract address, or ISO-4217 code for fiat |
| `payTo` | string | yes | Recipient address (or role constant like `"merchant"`) |
| `maxTimeoutSeconds` | number | yes | Authorization validity window |
| `extra` | object | no | Scheme-specific (EVM `exact`: `{name: "USDC", version: "2"}` EIP-712 domain info) |

### §2.2 PaymentPayload (v2)

| Field | Type | Required | Notes |
|---|---|---|---|
| `x402Version` | number | yes | `2` |
| `resource` | object | no | ResourceInfo `{url, description?, mimeType?}` |
| `accepted` | PaymentRequirements | yes | The requirement the payer chose (scheme/network live here — v2 has no top-level scheme/network) |
| `payload` | object | yes | Scheme-specific. EVM `exact`: `{signature, authorization: {from, to, value, validAfter, validBefore, nonce}}` — an EIP-3009 `TransferWithAuthorization` |
| `extensions` | object | no | Protocol extensions data |

### POST /verify — response

```json
{ "isValid": true,  "payer": "0x857b…" }
{ "isValid": false, "invalidReason": "insufficient_funds", "payer": "0x857b…" }
```
TS `VerifyResponse`: `{ isValid: boolean; invalidReason?: string; invalidMessage?: string; payer?: string; extensions?: Record<string,unknown>; extra?: Record<string,unknown>; }`.
Standard `invalidReason` codes (spec §9): `insufficient_funds`,
`invalid_exact_evm_payload_signature`, `invalid_exact_evm_payload_authorization_value_mismatch`,
`invalid_exact_evm_payload_authorization_valid_after` / `_valid_before`,
`invalid_exact_evm_payload_recipient_mismatch`, `invalid_network`, `invalid_payload`,
`invalid_payment_requirements`, `invalid_scheme`, `unsupported_scheme`,
`invalid_x402_version`, `unexpected_verify_error`. Note `invalidReason` is **omitted when valid** per spec.

### POST /settle — response

```json
{ "success": true,  "payer": "0x857b…", "transaction": "0x1234…", "network": "eip155:84532" }
{ "success": false, "errorReason": "insufficient_funds", "payer": "0x857b…", "transaction": "", "network": "eip155:84532" }
```
TS `SettleResponse`: `{ success: boolean; errorReason?: string; errorMessage?: string; payer?: string; transaction: string; network: Network; amount?: string; extensions?; extra?; }`.
`transaction` is required (empty string on failure); `network` is CAIP-2. `amount`
is optional — present for schemes like `upto` where settled ≠ authorized.
Spec §9 settle codes: `invalid_transaction_state`, `unexpected_settle_error`.

### GET /supported — response

```json
{
  "kinds": [
    { "x402Version": 2, "scheme": "exact", "network": "eip155:8453" },
    { "x402Version": 2, "scheme": "exact", "network": "eip155:84532" }
  ],
  "extensions": [],
  "signers": {
    "eip155:*": ["0xFACILITATOR_FEE_PAYER_ADDRESS"]
  }
}
```
TS `SupportedResponse`: `{ kinds: SupportedKind[]; extensions: string[]; signers: Record<string,string[]>; }`
(`signers`: CAIP-2 pattern → the fee-payer addresses the facilitator settles
with; resource servers read this at startup to validate support).

HTTP behavior SDKs expect: `Content-Type: application/json`; clients tolerate
non-200 responses carrying a JSON body; client-side timeout default **30s**
(TS `FacilitatorConfig.timeoutMs`, matching Go/Python). After settlement, the
resource server returns the `SettleResponse` to the buyer base64-encoded in the
`PAYMENT-RESPONSE` header.

## 3. What the facilitator does (semantics + trust)

**`/verify` — off-chain, moves no money.** For `exact`/EVM (spec §6.1.2):
1. validate EIP-712 signature (recover signer, must equal payer);
2. confirm payer token balance ≥ amount;
3. amount exactly matches requirements;
4. `validAfter ≤ now < validBefore`;
5. authorization params (to/asset) match requirements;
6. simulate `transferWithAuthorization` to ensure it would succeed.
Solana `exact` instead enforces strict instruction layout, fee-payer hygiene,
destination-ATA checks (§6.2).

**`/settle` — submits the payer's signed authorization to the chain and waits for
confirmation.** EVM: calls `transferWithAuthorization` on the ERC-20 contract;
facilitator pays the gas. Returns the tx hash. For already-on-chain schemes
(FastSet) it may be a verify+record pass-through.

**Trust assumptions:**
- Protocol is trust-minimizing by design: a facilitator **cannot move funds beyond
  the client's signed authorization** (EIP-3009 binds from/to/value/nonce; the
  contract, not the facilitator, enforces it).
- The **resource server trusts the facilitator** for: (a) honest verification,
  (b) actually submitting the settlement (a malicious facilitator could return
  `success: true` with a fake hash — mitigate by checking the hash on-chain),
  (c) not censoring. Buyers trust it to settle rather than grief; the payer's
  authorization is publicly replayable by anyone once signed, so a facilitator
  gains no theft power from holding it.
- Replay protection: EIP-3009 32-byte nonces (contract-enforced), time windows.
  **Double-settle is a no-op at the contract level** (nonce already used) on EVM.
  Solana has a known duplicate-submission race (RPC returns success on duplicates)
  — reference impls use a settlement cache (120s eviction, `duplicate_settlement`
  error) to close it.
- Some facilitators add policy: CDP runs KYT/OFAC checks on every transaction.

## 4. Authentication

**The protocol defines no auth on facilitator endpoints.** It is per-facilitator
policy, negotiated out-of-band. SDK support: TS `HTTPFacilitatorClient({ url,
createAuthHeaders, timeoutMs })` — `createAuthHeaders` returns per-endpoint
headers (`verify` / `settle` / `supported`); Go has the equivalent
`FacilitatorConfig`. Observed policies in the wild:
- Open (no key): PayAI, Dexter ("no account required"), public `x402.org` facilitator.
- Keyed: OpenZeppelin Relayer x402 plugin — `Authorization: Bearer <Relayer API key>`
  per endpoint; CDP production facilitator — CDP API key; Stack — no key for
  standard verify/settle, but agent API key + verified vendor domain required for
  gas sponsorship.

**Decision for AwLPay:** open or keyed is our call. If keyed, document the header
(e.g. `Authorization: Bearer <key>`) and verify/settle/supported separately — all
major SDK clients can inject it.

## 5. How a seller points at a custom facilitator — CORRECTION

**There is no `facilitator` field in standard `PaymentRequirements` (v1 or v2).**
The v2 spec's requirement fields are exactly: scheme, network, amount, asset,
payTo, maxTimeoutSeconds, extra (§5.1.2, verified against the spec table). The
buyer never learns the facilitator URL from the 402.

Pointing is **resource-server-side configuration** in the seller's middleware:

```ts
// TypeScript
const facilitator = new HTTPFacilitatorClient({
  url: "https://awlpay.fly.dev",          // <-- our facilitator base URL
  createAuthHeaders: async () => ({
    verify: { Authorization: "Bearer <redacted>" },
    settle: { Authorization: "Bearer <redacted>" },
  }),
});
const server = new x402ResourceServer(facilitator).register("eip155:8453", new ExactEvmScheme());
app.use(paymentMiddleware({ "GET /weather": { accepts: [...], ... } }, server));
```
```go
// Go
facilitator := x402http.NewHTTPFacilitatorClient(&x402http.FacilitatorConfig{ URL: "https://awlpay.fly.dev" })
```
Per-network routing is a known pattern (route Base → one facilitator, Avalanche →
another; `facilitator_by_network` in uvd-x402-sdk-python). If AwLPay ever wants
per-`accepts[]`-entry facilitator hints, the sanctioned vehicle is a custom
`extra` field, not a new top-level field.

## 6. v1 vs v2 — which to implement

| | v1 (legacy) | v2 (current) |
|---|---|---|
| `x402Version` | 1 | 2 |
| Amount field | `maxAmountRequired` | `amount` |
| Network id | chain slug (`"base-sepolia"`, `"base"`) | CAIP-2 (`"eip155:84532"`) |
| Requirement extras | `resource`, `description`, `mimeType`, `outputSchema` inline | moved to `ResourceInfo`; `extensions` object |
| Payment payload | `{x402Version, scheme, network, payload}` top-level | `{x402Version, resource?, accepted, payload, extensions?}` |
| Facilitator req body | `{x402Version, paymentHeader: "<base64 X-PAYMENT>", paymentRequirements}` | `{x402Version, paymentPayload: {...}, paymentRequirements}` |
| Settle response | `{success, error, txHash, networkId}` | `{success, errorReason, payer, transaction, network}` |
| Client→server header | `X-PAYMENT` | `PAYMENT-SIGNATURE` |
| Receipt header | `X-PAYMENT-RESPONSE` | `PAYMENT-RESPONSE` |
| `/supported` | `{kinds: [{scheme, network}]}` | `{kinds: [{x402Version, scheme, network, extra?}], extensions, signers}` |

**Recommendation: implement v2 as primary.** It is the current spec and what the
reference SDKs target. v1 remains widely deployed (PayAI advertises **both** kinds
for Base: `{"x402Version":1,"scheme":"exact","network":"base"}` and
`{"x402Version":2,"scheme":"exact","network":"eip155:8453"}`), so a v1
compatibility shim on /verify + /settle is cheap insurance if seller demand
appears — but v2 is where new integrations land. AwLPay already emits v2
`accepts[]`, so v2-first is consistent.

Network-identifier cautions for multi-rail: XRPL has no CAIP-2 form (its v1
identifier is used as-is; third-party SDKs keep XRPL on the v1 wire rather than
stuffing a v1 name into a v2 body). Stellar example seen: `"stellar:testnet"`.
Each new rail = one entry in `/supported`'s `kinds[]`.

## 7. Discovery — how AwLPay's facilitator gets found

- **Community list:** `x402-foundation/x402` docs maintain a facilitators table
  (`docs/dev-tools/facilitators.md`; current entries: CDP, PayAI, Corbits, Dexter,
  thirdweb, Stellar, Celo, Polygon, Meridian, Mogami, Fireblocks, NEAR, HPP, FTP
  Canton, **T54 XRPL** — note: `xrpl-x402.t54.ai`, XRP + RLUSD, a direct
  competitor/reference for our rail order). Listing is via PR/contribution.
- **Facilitator registries:** `facilitators.x402.watch` (19 facilitators, static
  HTML, CORS-open); awesome-x402 lists.
- **Machine discovery (ours to expose):** `GET /supported` (kinds/extensions/signers);
  `/.well-known/x402` or `/.well-known/x402-payment.json` manifest (used by agent
  skill files, e.g. Stack); `GET /discovery/resources` if we run a Bazaar.
- Resource directories (x402scan, x402-list, agentic.market, Ampersend) index
  **resources**, not facilitators — they matter for seller adoption, not for
  facilitator discovery.

## 8. Builder checklist (AwLPay facilitator service)

1. `GET /supported` → kinds per rail (x402Version 2, scheme `exact`, CAIP-2
   network), `extensions: []`, `signers` with our fee-payer addresses per CAIP
   pattern.
2. `POST /verify` → parse `{x402Version, paymentPayload, paymentRequirements}`;
   run scheme/network verification (signature, balance, amount, time window,
   param match, simulation); return `{isValid, invalidReason?, payer?}`.
3. `POST /settle` → re-verify, submit authorization on-chain, wait for
   confirmation, return `{success, errorReason?, payer, transaction, network}`.
   Handle the settle-after-timeout indeterminate case; guard duplicate settlement
   (nonce check / settlement cache).
4. Auth policy decision (open vs keyed); document header if keyed.
5. Per-(scheme, network) scheme modules behind the core; each rail needs its own
   verify+settle implementation and a funded fee-payer wallet.
6. v2 primary; v1 shim optional.
7. Discovery: `/supported`, `/.well-known/x402`, PR to the x402 docs facilitator
   list, submit to facilitators.x402.watch / awesome lists.
