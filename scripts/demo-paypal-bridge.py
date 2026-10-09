#!/usr/bin/env python3
"""Demo: an agent paying $X via PayPal through the awLPay fiat bridge.

Flow:
  1. Quote the fiat bridge offline (USD -> USDC, lab fee applied).
  2. If PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET are set: execute a real
     SANDBOX payout and print the Chamber-signed receipt.
     (Needs AWL_PAYPAL_PAYOUTS=1 — the money-movement gate — and the
     Payouts scope enabled on the sandbox REST app.)
  3. If credentials are missing: dry-run. Prints the quote, the exact
     order payload that WOULD be sent, and what to set for live sandbox.
     Touches no network. Exits 0 — dry-run is a documented path, not a
     failure.

Usage:
  python scripts/demo-paypal-bridge.py [AMOUNT_USD] [--recipient EMAIL]

Sandbox setup (developer.paypal.com):
  - Create a REST app in a SANDBOX business account.
  - Enable the Payouts scope on the app if you want payouts.
  - export PAYPAL_CLIENT_ID=... PAYPAL_CLIENT_SECRET=...
  - export AWL_PAYPAL_PAYOUTS=1   (money-movement gate)
"""

from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

from server import paypal as pp  # noqa: E402


def main(argv: list[str]) -> int:
    amount = "1.00"
    recipient = os.environ.get("PAYPAL_DEMO_RECIPIENT",
                               "sb-recipient@example.com")
    for a in argv[1:]:
        if a.startswith("--recipient="):
            recipient = a.split("=", 1)[1]
        elif not a.startswith("--"):
            amount = a

    print("=== awLPay fiat bridge demo (PayPal sandbox rail) ===")
    try:
        usd_cents = pp.usd_to_cents(amount)
    except ValueError as e:
        print("bad amount: %s" % e, file=sys.stderr)
        return 2
    if usd_cents <= 0:
        print("amount must be positive", file=sys.stderr)
        return 2

    # 1. Quote — pure offline math, always works.
    q = pp.quote_fiat_bridge(usd_cents)
    print("quote: $%s USD in -> %s micro-USDC out "
          "(fee %s, %s)" % (
              pp.cents_to_usd_str(q["usd_cents"]),
              f"{q['usdc_micro']:,}",
              pp.cents_to_usd_str(q["fee_cents"]),
              q["fee_status"]))
    print("rate: %s" % q["rate"])

    # Router: show the fiat path the anything-to-anything router finds.
    from server.router import ConverterRouter
    from server.oracle import default_mock_oracle
    path = ConverterRouter().find_path("paypal", "USD", "base", "USDC",
                                       default_mock_oracle())
    print("router path paypal/USD -> base/USDC: %s" % (
        " -> ".join("%s/%s(%s)" % (s["chain"], s["token"], s["hop"])
                    for s in path) if path else "NONE (refused)"))

    # 2. Credentials?
    try:
        pp.get_paypal_credentials()
        have_creds = True
    except RuntimeError as e:
        have_creds = False
        print("\n--- DRY RUN (no network touched) ---")
        print(str(e))
        print("would-be order payload for $%s:" % amount)
        print(json.dumps({
            "intent": "CAPTURE",
            "purchase_units": [{
                "amount": {"currency_code": "USD",
                           "value": pp.cents_to_usd_str(usd_cents)}}]},
            indent=2))
        print("for live sandbox: export PAYPAL_CLIENT_ID / "
              "PAYPAL_CLIENT_SECRET (sandbox REST app) and "
              "AWL_PAYPAL_PAYOUTS=1, then re-run.")
        return 0

    # 3. Live sandbox payout.
    print("\n--- LIVE SANDBOX ---")
    ok, reason = pp.payout_allowed()
    if not ok:
        print("gate: %s" % reason)
        print("export AWL_PAYPAL_PAYOUTS=1 to execute the sandbox payout.")
        return 0
    adapter = pp.PayPalAdapter()
    print("creating sandbox payout of $%s to %s ..." % (amount, recipient))
    try:
        out = adapter.create_payout(recipient, usd_cents,
                                    note="awLPay fiat-bridge demo")
    except RuntimeError as e:
        print("payout failed cleanly: %s" % e)
        return 1
    print("batch_id=%s status=%s" % (out["batch_id"], out["batch_status"]))
    for _ in range(6):
        time.sleep(5)
        st = adapter.get_payout(out["batch_id"])
        print("  poll: %s" % st["batch_status"])
        if st["batch_status"] in ("SUCCESS", "DENIED", "FAILED"):
            break
    receipt = pp.bridge_receipt("payout", out["batch_id"], usd_cents,
                                "out", extra={"recipient": recipient,
                                              "quote": q})
    env = pp.sign_bridge_receipt(receipt)
    print("\nChamber-signed receipt:")
    print(json.dumps(env, indent=2))
    print("\nSANDBOX ONLY — no real money moved.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
