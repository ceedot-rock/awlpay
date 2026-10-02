# Bitcoin on-chain payment verification — technical brief
**Date:** 2026-10-02 · **For:** AwLPay (Python x402 server), read-only verification
**Sources checked today:** Blockstream Esplora `API.md` (master, fetched 2026-10-02),
PyPI + empirical test of `embit` 0.8.0 against a hand-rolled BIP-173/350 reference.

## 1. API: Blockstream Esplora (primary), mempool.space (fallback)

Public base URLs (verbatim from Esplora API.md):
- Mainnet: `https://blockstream.info/api`
- Testnet (testnet3): `https://blockstream.info/testnet/api`
- Signet: `https://blockstream.info/signet/api`
- Docs: https://github.com/blockstream/esplora/blob/HEAD/API.md

Same path shapes work on `https://mempool.space/api` (fallback — keep in config).

Confirmed endpoints (all `GET`, JSON over REST):
| Endpoint | Returns |
|---|---|
| `/tx/:txid` | txid, version, locktime, size, weight, fee, **vin[]**, **vout[]**, status |
| `/tx/:txid/status` | `{confirmed, block_height?, block_hash?, block_time?}` — lightweight poll target |
| `/address/:address` | `{address, chain_stats, mempool_stats}` — each stats object: `{tx_count, funded_txo_count, funded_txo_sum, spent_txo_count, spent_txo_sum}` (sats) |
| `/tx/:txid/outspends` | per-output spend status `{spent, txid?, vin?, status?}` — useful for double-spend watching |
| `/blocks/tip/height` | bare integer (for computing confirmations) |

`GET /tx/:txid` on an unknown txid → **404**. Amounts are always **satoshis** (integers).

### vout[] shape (the fields that matter)
```json
{
  "scriptpubkey": "0014751e76e8199196d454941c45d1b3a323f1433bd6",
  "scriptpubkey_asm": "OP_0 OP_PUSHBYTES_20 751e76e8199196d454941c45d1b3a323f1433bd6",
  "scriptpubkey_type": "v0_p2wpkh",
  "scriptpubkey_address": "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4",
  "value": 100000
}
```
(`scriptpubkey_type` values seen: `p2pkh`, `p2sh`, `v0_p2wpkh`, `v0_p2wsh`, `v1_p2tr`, `op_return`, …;
`scriptpubkey_address` is absent for non-standard/OP_RETURN outputs.)

### vin[] shape (needed for the RBF check)
Each element carries `txid`, `vout`, `is_coinbase`, `scriptsig`, `scriptsig_asm`,
**`sequence`** (integer), `witness[]`, `prevout` (same shape as vout).

### status shape
```json
{ "confirmed": true, "block_height": 870000, "block_hash": "0000000000…", "block_time": 1727… }
```
Unconfirmed: `{ "confirmed": false }` (other fields absent/null).

**RBF flag: Esplora has none.** The documented field list for `/tx` contains no
`rbf` field (verified against API.md master today). Detect BIP-125 opt-in via
`vin[].sequence`: any input with `sequence < 0xfffffffe` (4294967294) signals
explicit replaceability. (Inherited signaling — an unconfirmed RBF-signaling
ancestor — requires walking `vin[].txid`s; for 0-conf acceptance, checking the
tx's own inputs is the standard cheap screen.)

## 2. Testnet choice: use **signet**, not testnet3

| Network | Esplora | Faucets | Notes |
|---|---|---|---|
| **signet** (recommended) | `https://blockstream.info/signet/api` | `alt.signetfaucet.com`; `mempool.space/signet/faucet` (login required) | Stable, signed blocks, no reorg/drought games |
| testnet3 | `https://blockstream.info/testnet/api` | `coinfaucet.eu/en/btc-testnet`, `tbtc.bitaps.com` (often dry) | Degraded: blockstorms, difficulty-reset games, faucet scarcity |
| testnet4 | `https://mempool.space/testnet4/api` (Blockstream's `/testnet/api` is still testnet3 per their docs) | `mempool.space/testnet4/faucet` (login required), testnet4.dev | Newer; use only if you specifically need testnet4 |

Recommendation: develop/test on **signet** (predictable), run mainnet for real.
Signet and testnet share address prefixes (`tb1…`, `m…`/`n…`, `2…`).

## 3. Address formats to accept + Python validation

Mainnet: P2PKH `1…` (0x00), P2SH `3…` (0x05), P2WPKH/P2WSH `bc1q…` (bech32, v0),
P2TR `bc1p…` (**bech32m**, BIP-350, v1). Testnet/signet: `tb1q…`, `tb1p…`,
`m…`/`n…` (0x6f), `2…` (0xc4).

**Recommended: `embit` 0.8.0 (`pip install embit`) — verified working today.**
`embit.script.address_to_scriptpubkey(addr)` raises on invalid input and
correctly handles bech32 **and** bech32m (P2TR). Cross-checked against an
independent hand-rolled BIP-173/350 implementation: identical results on
P2WPKH/P2WSH/P2TR/mainnet/testnet vectors.

Two caveats (both verified empirically):
1. It accepts **any** known network's prefixes/hrp — it does not enforce "this
   address is for mainnet". Add an explicit network check: segwit hrp must equal
   the active network's (`bc` main, `tb` test/signet, `bcrt` regtest); base58
   version byte must equal the active network's p2pkh/p2sh byte
   (main `00`/`05`; test+signet `6f`/`c4`).
2. The classic hand-rolled `bech32_decode` trap: bech32-only decoders **reject
   valid Taproot (`bc1p…`) addresses** because v1+ uses bech32m (different
   checksum constant). Do not roll your own with the `bech32` PyPI package's
   legacy `bech32_decode` alone — embit already handles this.

## 4. Confirmation policy for ~1-cent payments

What processors do: exchanges settled at 6 → now mostly **1–3 confirmations**
(Binance 1, Coinbase 2, Kraken 3, OKX/Bitget 1); Coinbase Commerce requires
on-chain confirmations before crediting; BTCPay Server marks an invoice
complete at its configured confirmation count (default 1, shows 0-conf as
paid-pending); BitPay historically offered tiered speeds (0-conf "high",
1-conf "medium", 6-conf "low" risk). The industry line for small amounts:
**0-conf is acceptable when the double-spend cost exceeds the payment** —
for 1¢, it always does.

Recommended two-tier policy for AwLPay BTC:
- **Accepted-pending (0-conf):** tx found in mempool, `sum(vout.value where
  scriptpubkey_address == payTo) >= N sats`, **no** input with
  `sequence < 0xfffffffe` (not RBF-signaling), fee rate sane (≥ ~1 sat/vB and
  not absurdly below mempool floor), seen propagated. Good enough to release
  the digital good / return the x402 receipt immediately.
- **Settled (1 conf):** `status.confirmed == true` (confirmations =
  tip_height − block_height + 1 ≥ 1). Final bookkeeping; require 2–3 confs only
  for unusually large payments (operator-set threshold).

Wait ~30s after first mempool sighting before "accepted-pending" to let the
tx propagate (standard 0-conf practice).

## 5. Amount units

Satoshis, **integers only, never floats**. Esplora returns `value` as int.
Compare `sum >= expected_sats` with plain int arithmetic. 1 BTC = 100,000,000
sats — keep everything in sats end to end (quote → payTo → verify).

## 6. Rate limits (public Esplora)

Blockstream publishes **no official numbers**. Community experience: 429s under
burst load, occasionally even at modest rates; there is no `Retry-After`
guarantee. Practical rules:
- Keep ≤ **1–2 req/s** per server; put ~250–500 ms spacing in polling loops.
- Poll the light `/tx/:txid/status` (or `/address/:address` stats), not full
  `/tx`, while waiting; fetch full `/tx` once when first seen/confirmed.
- Cache confirmed results **indefinitely** (block data never changes).
- Exponential backoff + jitter on 429/5xx; fail over to `mempool.space/api`
  (same paths).
- Never retry 400/404 (unknown txid = "not found yet", poll on schedule).
- For production volume, self-host esplora/electrs — Blockstream's own docs
  recommend it.

## 7. Verification checklist (per payment)

1. Sanitize: txid is 64 lowercase hex; validate `payTo` with embit **and**
   network-match (hrp/version byte) before any query — a mainnet address
   queried on signet 404s and vice versa.
2. `GET /tx/:txid` → 404 means unknown (keep polling until invoice expiry);
   200 means known.
3. Amount: `total = sum(vout["value"] for vout if vout.get("scriptpubkey_address") == payTo)`; require `total >= expected_sats` (int compare). Match against the *quoted* address, not any address.
4. 0-conf screen: `status.confirmed == false` → check every `vin.sequence >= 0xfffffffe`
   (reject/flag RBF-signaling), fee rate sane; optionally wait ~30 s propagation.
5. Settlement: `status.confirmed == true` → confirmations = `GET /blocks/tip/height − block_height + 1`; require ≥ 1 (≥ 2–3 for large amounts).
6. Reorg safety: after "settled", re-check `/tx/:txid/status` block_hash is still
   in best chain on later polls for high-value payments.
7. Optional: `/tx/:txid/outspends` to observe if our output was later spent
   (informational; receipt already proven).

Poll cadence suggestion: every 15–30 s until first seen, then every 60 s until
1 conf, then stop (cache result). Invoice expiry bounds the whole loop.
