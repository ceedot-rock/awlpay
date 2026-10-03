# awlpay (Python)

The 5-minute stablecoin wallet for AI agents. **Testnet by default.**
No seed phrases, no gas math, no rail selection.

```python
from awlpay import AgentWallet

w = AgentWallet(password="correct horse battery staple")
print(w.deposit_address("base"))  # fund with Base Sepolia USDC
print(w.balances())               # {"base": 0.0, "solana": 0.0}
receipt = w.pay("0xRecipient...", 1.00)  # auto-picks cheapest funded rail
print(receipt["tx_hash"])
```

Full walkthrough (install → fund → pay $1 → read the receipt):
[`../QUICKSTART.md`](../QUICKSTART.md)

## Safety model

- Wallet file at `~/.awlpay/wallets.enc`, AES-256-GCM, key from your
  password via PBKDF2-HMAC-SHA256 (600k iterations). Password never hits disk.
- Testnet default. Mainnet needs `network="mainnet"` **and**
  `confirm_mainnet="I UNDERSTAND"` (literal string).
- Lifetime spend cap (`max_spend_usd=100` default) + optional per-call
  `max_usd` on `pay()`. Caps are enforced before anything is signed.
- Plain transfers go straight to chain RPC — the AwLPay server is not involved.

## Install

```bash
pip install awlpay
```

## Develop

```bash
python -m venv .venv && .venv/bin/pip install -e ".[test]"
.venv/bin/pytest
```

## License

Apache-2.0 — Slid Phi Labs.
