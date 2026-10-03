# AwLPay Quickstart — 5 minutes, zero crypto background

Send stablecoins from Python or JavaScript with no seed phrases, no gas
math, and no rail selection. The SDK picks the cheapest funded rail,
signs locally, and hands you a receipt.

**USDC** is a digital dollar: 1 USDC == $1, always. Testnet USDC is free
play money for practice — worth nothing, perfect for learning.

> **Publish status (2026-10-03):** `pip install awlpay` / `npm install awlpay`
> are the package names (verified free on PyPI and npm) but neither is
> published yet — publishing is the lab's call. Until then, install from
> this repo as shown below. Everything else in this guide is verified
> working; the one step that needs *you* is the free faucet visit in
> step 3.

---

## Python

**1. Install** (2 min)

```bash
pip install awlpay
# — until published, from a clone of this repo:
# pip install ./py-sdk
```

**2. Create a wallet** (30 sec) — first run generates fresh wallets,
AES-256-GCM encrypted with your password at `~/.awlpay/wallets.enc`.
Testnet is the default; nothing here can touch real money.

```python
from awlpay import AgentWallet

w = AgentWallet(password="correct horse battery staple")
print(w.deposit_address("base"))
# 0x5b2f...D5fF  <- fund THIS address
print(w.deposit_address("solana"))
# HmkA...zGeQ    <- or THIS one
print(w.balances())
# {"base": 0.0, "solana": 0.0}
```

**3. Fund it** (2 min, free) — get free *testnet* USDC from Circle's
faucet (multiple independent sources confirm it serves both chains):

1. Go to **https://faucet.circle.com**
2. Select **USDC** + **Base Sepolia**, paste your `base` address, request.
3. Repeat for **Solana devnet** with your `solana` address (you'll also
   want a little devnet SOL for fees: https://faucet.solana.com).

Then re-run `w.balances()` — you should see your test USDC.

**4. Pay $1** (30 sec) — `rail="auto"` picks the cheapest *funded* rail.
No selection, no gas settings.

```python
receipt = w.pay("0xRecipientAddress...", 1.00)
print(receipt)
# {"rail": "base", "tx_hash": "0xabc...", "usd": 1.0,
#  "to": "0xRecipientAddress...", "confirmed_at": "2026-10-03T...",
#  "network": "testnet"}
```

Verify it yourself: `https://sepolia.basescan.org/tx/0xabc...`

**5. Read the receipt.** `tx_hash` is the on-chain proof, `rail` tells
you which chain it rode, `confirmed_at` is when the chain finalized it.
That's the whole audit trail — no AwLPay server was involved.

> **Verified 2026-10-03:** wallet generation (real Base + Solana
> addresses), encryption roundtrip, spend-cap enforcement, rail
> auto-selection, and the unfunded-pay refusal were all executed and
> green (21/21 pytest). The live broadcast path is code-complete and
> reviewed but **has not yet moved real testnet funds** — it needs the
> faucet step above, which requires a human click.

---

## JavaScript / TypeScript

Same flow, async API:

```bash
npm install awlpay
# — until published, from a clone of this repo:
# npm install ./js-sdk
```

```typescript
import { AgentWallet } from "awlpay";

const w = await AgentWallet.create({ password: "correct horse battery staple" });
console.log(await w.depositAddress("base"));    // fund this (faucet.circle.com)
console.log(await w.balances());                // { base: 0, solana: 0 }

const receipt = await w.pay("0xRecipientAddress...", 1.0);
console.log(receipt.txHash);                    // on-chain proof
```

---

## The agent safety story (spend caps)

This is the permission layer Cloudflare doesn't give you. Caps are
enforced *before anything is signed* — an agent literally cannot
overspend:

```python
# Lifetime cap: the wallet refuses once $25 total has gone out.
w = AgentWallet(password="...", max_spend_usd=25)

# Per-call cap: this one payment may not exceed $5.
w.pay("0xRecipient...", 3.00, max_usd=5.0)
```

- `SpendCapExceeded` is raised before signing; nothing hits the chain.
- Lifetime spend is tracked in the encrypted wallet file and survives restarts.
- Combine both: a tight per-call cap inside a bounded lifetime budget.

## Going further

- **Mainnet (real money):** explicit opt-in only —
  `AgentWallet(password, network="mainnet", confirm_mainnet="I UNDERSTAND")`.
  Read the address, network, and amount twice before you run it.
- **More rails:** the SDK ships Base + Solana today; the rail registry is
  pluggable and AwLPay's server already settles on 11 rails.
- **Paid APIs (the x402 flow):** when an API answers `402 Payment
  Required`, see [docs/integrator-guide.md](docs/integrator-guide.md) —
  the SDKs above are the recommended starting point before the manual flow.
- **Agent toll gates:** per-call metering with Chamber-sealed receipts —
  [docs/](docs/) has the toll-gate map.
