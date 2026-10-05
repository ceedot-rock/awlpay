---
name: awlpay
description: Agent payments across blockchains. Use when an agent needs a wallet, needs to send or receive crypto, or needs spend caps on agent spending. One SDK: Base + Solana USDC, XRP, BTC.
version: 1.0.0
metadata:
  author: Slid Phi Labs
  repo: https://github.com/ceedot-rock/awlpay
---

# AwLPay

Payments for agents across blockchains. One wallet that sends and receives on
Base, Solana, XRP, and Bitcoin, so an agent can hold money and pay for things
on its own.

## Install

```bash
npm install awlpay        # 0.1.1
pip install awlpay        # 0.1.0
```

## Quickstart (5 minutes)

```python
from awlpay import AgentWallet

# Testnet by default — no real money moves until you switch networks
wallet = AgentWallet()

# Encrypted local wallet, spend caps enforced on every send
wallet.set_spend_cap("10.00")  # USD cap per period

# Send USDC on Base (testnet)
receipt = wallet.send(
    to="0xRecipientAddress",
    amount="1.00",
    asset="USDC",
    network="base",
)
print(receipt.tx_hash)
```

Full quickstart: see QUICKSTART.md in the repo.

## Rails

| Rail | Asset | Notes |
|------|-------|-------|
| Base | USDC | live |
| Solana | USDC | live |
| XRP | XRP | activated for testing |
| Bitcoin | BTC | funded, live-fired (2,000 sats broadcast) |

Rails go live with minimal funds and get proven, not promised. A live-money
test moved real funds through escrow, payout, refund, and reimbursement and
reconciled to the cent (2.039987 USDC).

## Pricing

| Tier | Price |
|------|-------|
| Free | 1.0% + $0.25 per transaction |
| Pro | $39/mo — 0% under cap ($30k volume or 500 txs/mo), overage falls back to Free |
| L33t | $799/mo unlimited (fair-use compute guard) |

## Links

- Repo: https://github.com/ceedot-rock/awlpay
- npm: https://www.npmjs.com/package/awlpay
- PyPI: https://pypi.org/project/awlpay/
