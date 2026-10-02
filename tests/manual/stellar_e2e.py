#!/usr/bin/env python3
"""Stellar rail E2E (testnet only, throwaway wallets, run by hand).

Submits real testnet XLM transactions carrying the memo-hash challenge
binding, waits for finality, then runs them through
server.stellar.verify_stellar_payment.

  1. payment op: funder -> payee (both friendbot-funded), memo hash set
  2. create_account op: funder -> fresh address (documents that
     create_account funds XLM and must verify)

Wallets below are friendbot-funded throwaways from the 2026-10-02 rail
build. NEVER put a real seed in this file.
"""
import base64
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from stellar_sdk import Keypair, Server, TransactionBuilder, Network, Asset
from stellar_sdk.memo import HashMemo
from stellar_sdk.exceptions import NotFoundError

from server import stellar as stmod

HORIZON = "https://horizon-testnet.stellar.org"
SALT = "e2e-test-salt-2026-10-02"
PAY_XLM = "5"
PAY_STROOPS = 5 * 10_000_000

os.environ["AWL_STELLAR_MEMO_SALT"] = SALT


def friendbot(addr: str):
    url = "https://friendbot.stellar.org?addr=" + addr
    # friendbot 403s the stock urllib User-Agent; identify ourselves.
    req = urllib.request.Request(
        url, headers={"User-Agent": "awlpay-stellar-e2e/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def submit_envelope(envelope_xdr: str) -> dict:
    # Horizon POST via stdlib urllib: the requests-based SDK client
    # stalls on this runtime's egress proxy (read timeouts).
    data = urllib.parse.urlencode({"tx": envelope_xdr}).encode()
    req = urllib.request.Request(
        HORIZON + "/transactions", data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "User-Agent": "awlpay-stellar-e2e/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:800]
        raise RuntimeError("horizon submit %d: %s" % (e.code, body))


def main():
    server = Server(HORIZON)
    funder = Keypair.random()
    payee = Keypair.random()
    fresh = Keypair.random()
    print("funder:", funder.public_key)
    print("payee: ", payee.public_key)
    print("fresh: ", fresh.public_key)

    print("funding via friendbot...")
    friendbot(funder.public_key)
    friendbot(payee.public_key)

    # --- tx 1: payment op with memo-hash binding ---
    memo = stmod.memo_id(payee.public_key, str(PAY_STROOPS),
                         "stellar:testnet", SALT)
    print("memo_id:", memo)
    acct = server.load_account(funder.public_key)
    tx = (TransactionBuilder(acct, network_passphrase=Network.TESTNET_NETWORK_PASSPHRASE,
                             base_fee=100)
          .append_payment_op(payee.public_key, Asset.native(), PAY_XLM)
          .add_memo(HashMemo(bytes.fromhex(memo)))
          .set_timeout(60).build())
    tx.sign(funder)
    resp = submit_envelope(tx.to_xdr())
    txh1 = resp["hash"]
    print("payment tx submitted:", txh1)

    # --- tx 2: create_account op to a fresh address ---
    memo2 = stmod.memo_id(fresh.public_key, str(PAY_STROOPS),
                          "stellar:testnet", SALT)
    acct = server.load_account(funder.public_key)
    tx2 = (TransactionBuilder(acct, network_passphrase=Network.TESTNET_NETWORK_PASSPHRASE,
                              base_fee=100)
           .append_create_account_op(fresh.public_key, PAY_XLM)
           .add_memo(HashMemo(bytes.fromhex(memo2)))
           .set_timeout(60).build())
    tx2.sign(funder)
    resp2 = submit_envelope(tx2.to_xdr())
    txh2 = resp2["hash"]
    print("create_account tx submitted:", txh2)

    # confirm Horizon's create_account op field names (documents the
    # _find_pay_op decision)
    ops2 = server.operations().for_transaction(txh2).call()
    print("create_account op fields:",
          sorted(ops2["_embedded"]["records"][0].keys()))

    # wait for finality (FINALITY_LEDGERS deep)
    for txh in (txh1, txh2):
        for _ in range(60):
            time.sleep(5)
            cur = server.transactions().transaction(txh).call()
            latest = server.ledgers().order(desc=True).limit(1).call()
            seq = latest["_embedded"]["records"][0]["sequence"]
            if seq - cur["ledger"] >= stmod.FINALITY_LEDGERS:
                print(txh[:12], "final at ledger", cur["ledger"],
                      "(latest", seq, ")")
                break
        else:
            print("TIMEOUT waiting for finality on", txh)
            sys.exit(1)

    # verify both through the real code path (live Horizon, no mock)
    ok1, info1 = stmod.verify_stellar_payment(
        txh1, "stellar:testnet", PAY_STROOPS, payee.public_key, set(),
        memo_salt_val=SALT)
    print("payment verify ok:", ok1)
    print(json.dumps(info1, indent=2)[:500])
    assert ok1, info1
    assert info1["op_kind"] == "payment"

    ok2, info2 = stmod.verify_stellar_payment(
        txh2, "stellar:testnet", PAY_STROOPS, fresh.public_key, set(),
        memo_salt_val=SALT)
    print("create_account verify ok:", ok2)
    print(json.dumps(info2, indent=2)[:500])
    assert ok2, info2
    assert info2["op_kind"] == "create_account"

    print("E2E PASS")
    print("payment tx:        ", txh1)
    print("create_account tx: ", txh2)


if __name__ == "__main__":
    main()
