# Security Policy

AwLPay moves value behind an x402 payment gate with Ed25519-signed
receipts. A bug that lets someone get a quote that should refuse, skip
the payment gate, forge a receipt, or move funds the law says must not
move is a security issue, not a normal bug.

## Reporting a vulnerability

Please do not open a public issue for security problems.

- Use GitHub's private vulnerability reporting on this repository
  (Security tab, "Report a vulnerability")
- Or email: corey@slidphilabs.com with the subject line `AwLPay security`

Include the affected route or file, steps or inputs to reproduce, and
what you expected versus what happened.

You can expect an acknowledgement within 3 business days. We will keep
you updated while we investigate and credit you in the changelog unless
you prefer to stay anonymous.

## In scope

- Payment-gate bypass: any path to `/api/pay/execute` without a valid
  x402 payment proof (outside the documented `AWL_LOCAL_DEV=1` test
  bypass)
- Fee-law integrity: quotes that under- or over-charge versus
  `server/fees.py`
- Receipt forgery or chamber-signature verification weaknesses
- Payment-hash or idempotency-key replay that double-executes
- The `awlpay` npm and PyPI packages

## Out of scope

- Operator deployments we do not run (your own keys, your own config)
- Social engineering, spam, or denial-of-service against hosted demos
- Mainnet behavior: this repo hard-disables mainnet by design
