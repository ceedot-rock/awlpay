# Tron TRC-20 USDT Payment Verification — Technical Brief

**Method: read-only.** One POST to TronGrid per payment. Server never signs, holds no keys, pays no fees (energy/bandwidth irrelevant — we never submit transactions).

## 1. The endpoint (verified live 2026-10-02)

- **URL:** `POST https://api.trongrid.io/wallet/gettransactioninfobyid`
- **Body:** `{"value": "<64-char hex txid>"}`, Content-Type `application/json`
- **Auth:** works without a key at low rate; send `TRON-PRO-API-KEY: <key>` header (free TronGrid key) for higher limits.
- **Missing/unknown tx:** returns **HTTP 200 with body `{}`** — not a 404. Empty object = not found (invalid hash, or tx not yet executed). Must be treated as "not paid."

### Full response shape (real mainnet USDT transfer, tx `d94fad38fc0988bdfe4529f87cfa04524355b8b4d69f7285c26eb91b88b08332`, block 86765937, fetched today)

```json
{
  "id": "d94fad38fc0988bdfe4529f87cfa04524355b8b4d69f7285c26eb91b88b08332",
  "blockNumber": 86765937,
  "blockTimeStamp": 1759416300000,
  "fee": 345000,
  "contract_address": "a614f803b6fd780986a42c78ec9c7f77e6ded13c",
  "contractResult": ["SUCCESS"],
  "receipt": {
    "energy_usage": 64284,
    "origin_energy_usage": 1,
    "energy_usage_total": 64285,
    "net_fee": 345000,
    "result": "SUCCESS",
    "energy_penalty_total": 49635
  },
  "log": [{
    "address": "a614f803b6fd780986a42c78ec9c7f77e6ded13c",
    "topics": [
      "ddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
      "000000000000000000000000e726273ed105e499759aab49d6309e9c1a1c86b0",
      "00000000000000000000000039082fc464af08d9a7002fefc4dbf7da5eec7f2c"
    ],
    "data": "000000000000000000000000000000000000000000000000000000022d613580"
  }]
}
```

Top-level keys observed: `id, blockNumber, blockTimeStamp, fee, contract_address, contractResult, receipt, log` (`internal_transactions` may also appear on some txs). This single call is sufficient — `gettransactionbyid` (the tx *body*) is not needed for verification.

## 2. Decoding the TRC-20 Transfer

- **Transfer event signature topic:** `ddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef` (= keccak256 of `Transfer(address,address,uint256)`).
- **topics[1]** = sender, **topics[2]** = recipient — each a 32-byte left-padded word; the address is the **last 20 bytes** (hex chars 24–64 of the word).
- **`log.address`** = emitting contract's **20-byte hex, WITHOUT the `41` prefix** (gotcha discovered live: `a614f803b6fd780986a42c78ec9c7f77e6ded13c`). To compare with base58 forms, prepend `"41"`.
- **Amount:** `data` is a 32-byte big-endian uint256: `int(data, 16)` in smallest units. USDT = **6 decimals** → divide by 1,000,000. Example above: `0x22d613580` = 9,351,280,000 = **9,351.28 USDT**.
- **Address conversion:** Tron hex = 21 bytes `41` + 20-byte address; base58check (`T…`) = Base58Check of those 21 bytes (double-SHA256 checksum). I verified the round-trip in Python: base58check-decode of `TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t` → `41a614f803b6fd780986a42c78ec9c7f77e6ded13c` ✔.

## 3. Contract addresses

| Network | USDT (TRC-20) | Decimals | Notes |
|---|---|---|---|
| Mainnet | `TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t` | 6 | ✔ confirmed (multiple sources + own base58check decode). Hex: `41a614f803b6fd780986a42c78ec9c7f77e6ded13c` |
| Nile testnet | `TXYZopYRdj2D9XRtbG411XZZ3kM5VkAeBf` | 6 | Community-documented (3+ independent repos/skills); NOT Tron-official — re-confirm on nile.tronscan.org before trusting |
| Shasta testnet | `TG3XXyExBkPp9nzdajDZsozEu4BkaSJozs` | 6 | Well-known test USDT-like token |

## 4. Nile testnet

- **RPC:** `https://nile.trongrid.io` (full REST API, same paths as mainnet) — also `https://nile.trongrid.io/jsonrpc` for EVM-style JSON-RPC; gRPC `grpc.nile.trongrid.io:50051`. (Note: `tronpy`'s `network="nile"` preset points at `https://api.nileex.io`.)
- **Explorer:** https://nile.tronscan.org/
- **Faucet (test TRX):** https://nileex.io/join/getJoinPage — 2,000 test TRX/day/address; same page also dispenses test USDT (one guide says 50,000). Alternative: TRON Discord faucet channel, `!nile_usdt <YOUR_ADDRESS>`.
- **If the Nile USDT address above can't be confirmed:** deploy your own TRC-20 on Nile (TronIDE at tronide.io or TronBox, standard OpenZeppelin ERC-20 compiles for TVM) using faucet TRX, and use its address. Or read the actual token contract from the `log.address` field of your faucet-claim receipt.

## 5. Gotchas

1. **`{}` = not found** (HTTP 200). Never treat a 200 alone as success.
2. **`receipt.result` must be exactly `SUCCESS`.** Full enum (developers.tron.network/docs/errors): `DEFAULT, SUCCESS, REVERT, BAD_JUMP_DESTINATION, OUT_OF_MEMORY, PRECOMPILED_CONTRACT, STACK_TOO_SMALL, STACK_TOO_LARGE, ILLEGAL_OPERATION, STACK_OVERFLOW, OUT_OF_ENERGY, OUT_OF_TIME, JVM_STACK_OVER_FLOW, UNKNOWN, TRANSFER_FAILED, INVALID_CODE`. On any non-SUCCESS, state changes were rolled back — no Transfer log exists. **Do not retry a failed tx with the same payload** (txids are single-use).
3. **Check the log, not the tx body.** A `TriggerSmartContract` call to USDT with the right `transfer(to, amount)` calldata can still REVERT — only the emitted Transfer log proves it happened.
4. **Multiple logs possible** (proxies, multi-hop). Iterate all logs; accept the one matching contract + signature + our address.
5. **`log.address` lacks the `41` prefix** — normalize before comparing.
6. **USDT quirk (OpenZeppelin's SafeTRC20 note):** mainnet USDT's `transfer()` returns `false` even on success. Irrelevant for log-based verification — but don't ever rely on `contractResult`/return booleans.
7. **Confirmations:** `blockNumber` vs `POST /wallet/getnowblock` (`block_header.raw_data.number`). Tron finality is fast (~19 blocks); require e.g. ≥20 confirmations for high-value payments. Solidity-node irreversibility check optional via the same path on a solidity host.
8. **Replay:** track consumed txids; one txid = one payment.
9. **Rate limits:** add `TRON-PRO-API-KEY` for production polling.

## 6. Python library: tronpy — recommended ✔

- `pip install tronpy` (pure-Python; needed a venv on this VM due to Debian's externally-managed environment).
- **Address conversion — verified working, fully offline:**
  - `from tronpy.keys import to_base58check_address, to_hex_address`
  - `to_base58check_address("41a614f803b6fd780986a42c78ec9c7f77e6ded13c")` → `TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t` ✔
  - `Tron().to_base58check_address(...)` also works; `generate_address()` is offline ✔
  - Helpers also available: `is_base58check_address`, `is_hex_address`, `public_key_to_base58check_addr`.
- **HTTP provider presets (verified):** `Tron()` → `https://api.trongrid.io`; `Tron(network="nile")` → `https://api.nileex.io`. tronpy wraps the FullNode HTTP API, so it can issue the `gettransactioninfobyid` POST too — but plain `requests`/`urllib` is equally fine and has fewer dependencies.

## Verification checklist (for AwLPay's verify path)

1. `POST https://api.trongrid.io/wallet/gettransactioninfobyid` `{"value": txid}` → non-empty JSON (else: unknown tx).
2. `receipt.result == "SUCCESS"` (else: failed, reject).
3. Scan `log[]` for an entry with: `address` == `a614f803b6fd780986a42c78ec9c7f77e6ded13c` (USDT 20-byte hex), `len(topics) == 3`, `topics[0]` == `ddf252ad…b3ef`.
4. `to_base58check_address("41" + topics[2][24:])` == our `T…` receive address.
5. `int(data, 16) >= required_amount_raw` (6-decimal units).
6. Optional: `getnowblock` − `blockNumber` ≥ confirmation threshold; txid not previously consumed.

**Caveats:** The Nile USDT address is community-documented, not Tron-official — confirm on nile.tronscan.org before using in tests. Rate-limit behavior without an API key was not stress-tested; one-off calls worked fine today."}, "created_at": "2026-10-02T19:48:53.441846134+00:00"}
