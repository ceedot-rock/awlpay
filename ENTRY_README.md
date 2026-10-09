# awLPay Fiat Bridge — PayPal rail for agents

**Hackathon entry: PayPal agentic commerce ($5,000 award track)**

## What it is

Agents can't open bank accounts. Most of the world's commerce still runs
on fiat. The awLPay fiat bridge closes that gap: a PayPal rail inside
awLPay's anything-to-anything payment router, so an agent can quote,
send, and receive real US dollars — and convert them into USDC on any
supported chain — through one API.

## Why agents need fiat rails

Crypto rails move tokens between wallets. But the merchant, the
supplier, and the customer's refund all live in fiat. An agent that can
only spend crypto can't buy from 90% of the internet. The fiat bridge
gives agents a dollar-denominated on/off ramp with the same guarantees
as every awLPay rail:

- **Quoted in integer cents** — no float math anywhere in the money path.
- **Anything-to-anything routing** — USD is a first-class node in the
  ConverterRouter graph. `USD(paypal) -> USDC(base) -> ETH(base)` resolves
  like any other path; unpriced tokens still refuse.
- **Chamber-signed receipts** — every bridge payment produces an
  Ed25519-signed attestation envelope, same as the chain rails.
- **Sandbox-only by code** — the production PayPal endpoint is
  hard-refused; no env var can override it. Payouts additionally require
  an explicit gate flag instead of silently dry-running.

## What's in this entry

| File | What |
|---|---|
| `server/paypal.py` | PayPalAdapter: Orders v2 (create/get/capture) + Payouts (create/get), OAuth2, sandbox law, integer-cents math, signed receipts |
| `server/router.py` | Fiat graph wiring: `paypal`/`USD` node, `fiat` hop edges to every USDC chain |
| `server/oracle.py` | USD = 1.0 as the unit of account (definitional, not a market price) |
| `tests/test_paypal_rail.py` | 27 tests, all mocked, zero network |
| `scripts/demo-paypal-bridge.py` | Agent demo: quote -> sandbox payout -> signed receipt |

## Run it

```bash
cd ~/workspace/awlpay
venv/bin/python -m pytest tests/test_paypal_rail.py -q   # 27 tests, no network
venv/bin/python scripts/demo-paypal-bridge.py 2.50      # dry-run (no creds needed)
```

Live sandbox (needs your keys — nothing here will run without them):

```bash
export PAYPAL_CLIENT_ID=... PAYPAL_CLIENT_SECRET=...   # sandbox REST app, developer.paypal.com
export AWL_PAYPAL_PAYOUTS=1                            # money-movement gate
venv/bin/python scripts/demo-paypal-bridge.py 1.00 --recipient you@example.com
```

The sandbox REST app needs the **Payouts** scope enabled for payouts.
Orders v2 create works with default scopes; capture needs buyer approval
(a browser step — the demo prints the approve URL).

## Measured, not claimed

- 27/27 rail tests green; full repo suite 205 passed, 1 skipped, zero
  regressions from the graph/oracle changes.
- Quote math verified: $100.00 -> 50c fee (0.5% free tier) -> 9,950c ->
  99,500,000 micro-USDC, all integer.
- Router: `paypal/USD -> base/USDC` resolves `[origin, fiat]`; pre-existing
  exact hop sequences (`ETH->USDC(base)` = origin/swap/bridge) unchanged.
- Demo dry-run exits 0 with no network I/O and a clear credential message.

## What's next (with Corey)

- Live sandbox verification with his PayPal sandbox REST app credentials.
- Production PayPal is deliberately unwired — that flip is a code change
  plus his explicit per-charge approval, never a config toggle.
