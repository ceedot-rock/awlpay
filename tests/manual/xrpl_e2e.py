#!/usr/bin/env python3
"""XRPL rail E2E (testnet only, throwaway wallets, run by hand).

Submits a real testnet XRP Payment carrying the InvoiceID challenge
binding, waits for validation, then runs it through
server.xrpl.verify_xrpl_payment.

Wallets below are faucet-funded throwaways from the 2026-10-02 rail
build. NEVER put a real seed in this file.
"""
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from xrpl.wallet import Wallet
from xrpl.models.transactions import Payment
from xrpl.transaction import sign

from server import xrpl as xrplmod

PAYER_SEED = "sEd7dMjuLBzuYdoddd5cpiBwmQZtXrm"
PAY_TO = "rL9zi7gdzYbXizDP5CgKRMD3jVkMbpMwR9"
AMOUNT_DROPS = "10000"
RPC = "https://s.altnet.rippletest.net:51234"
SALT = "e2e-test-salt-2026-10-02"

os.environ["AWL_XRPL_INVOICE_SALT"] = SALT


def rpc(method, params):
    body = json.dumps({"method": method, "params": [params]}).encode()
    req = urllib.request.Request(
        RPC, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def main():
    invoice = xrplmod.invoice_id(PAY_TO, AMOUNT_DROPS, "xrpl:1", SALT)
    print("invoiceId:", invoice)

    wallet = Wallet.from_seed(PAYER_SEED)
    print("payer:", wallet.classic_address)

    # account_info for the sequence number
    acct = rpc("account_info", {"account": wallet.classic_address,
                                "ledger_index": "validated"})
    seq = acct["result"]["account_data"]["Sequence"]
    fee = rpc("fee", {})["result"]["drops"]["minimum_fee"]
    print("seq:", seq, "fee:", fee)

    pay = Payment(account=wallet.classic_address,
                  destination=PAY_TO,
                  amount=AMOUNT_DROPS,
                  invoice_id=invoice,
                  sequence=seq,
                  fee=fee)
    signed = sign(pay, wallet)
    blob = signed.blob()
    sub = rpc("submit", {"tx_blob": blob})
    eng = sub["result"]["engine_result"]
    txh = sub["result"]["tx_json"]["hash"]
    print("submit:", eng, "tx:", txh)
    assert eng == "tesSUCCESS", sub

    # wait for validation
    for _ in range(30):
        time.sleep(4)
        res = rpc("tx", {"transaction": txh})["result"]
        if res.get("validated"):
            print("validated at ledger", res.get("ledger_index"))
            break
    else:
        print("TIMEOUT waiting for validation")
        sys.exit(1)

    # verify through the real code path
    ok, info = xrplmod.verify_xrpl_payment(
        txh, "xrpl:1", int(AMOUNT_DROPS), PAY_TO, set(),
        invoice_salt_val=SALT)
    print("verify ok:", ok)
    print(json.dumps(info, indent=2)[:600])
    assert ok, info
    print("E2E PASS — tx:", txh)


if __name__ == "__main__":
    main()
