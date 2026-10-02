"""awLPay XRPL rail tests: verifier checklist against mocked ledger data.

Covers (all with a fake RPC — no network):
  (a) pure helpers: r-address validation, drops math, invoiceId binding
  (b) verify_xrpl_payment: happy path, then each refusal in the §2
      checklist — wrong type, wrong destination, partial-payment flag,
      underpaid, IOU asset, bad/missing InvoiceID, unvalidated ledger,
      unknown tx, replay
  (c) x402 wiring: parse_x_payment accepts xrpl:1 txHash proofs,
      payment_terms advertises the rail when configured

No mainnet, no real funds, no network. The live testnet E2E lives in
tests/manual/xrpl_e2e.py (throwaway wallets, run by hand).
"""

from __future__ import annotations

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

from server import xrpl as xrplmod  # noqa: E402
from server import x402 as x402mod  # noqa: E402
from server.oracle import MockOracle  # noqa: E402

# Throwaway r-addresses (test only — never real wallets).
PAY_TO = "rL9zi7gdzYbXizDP5CgKRMD3jVkMbpMwR9"
PAYER = "rwm8z8LTYeNDRSafzrmfUt4eeE3vBftfsS"
OTHER = "rPT1Sjq2YGrkzdbFRieFYDcZgbFQCvSQ2d"
TXH = "A" * 64
SALT = "test-salt-only"
MIN_DROPS = 500_000


def _tx_result(**over):
    """A validated XRP Payment tx result shaped like the ledger's `tx`."""
    tx = {
        "TransactionType": "Payment",
        "Account": PAYER,
        "Destination": PAY_TO,
        "Amount": str(MIN_DROPS),
        "Flags": 0,
        "InvoiceID": xrplmod.invoice_id(PAY_TO, str(MIN_DROPS),
                                        "xrpl:1", SALT),
        "date": int(time.time()) - 946684800 - 60,  # 1 min ago
    }
    tx.update(over)
    return {
        "validated": True,
        "ledger_index": 21224500,
        "tx": tx,
        "meta": {"delivered_amount": str(MIN_DROPS)},
    }


def _rpc_ok(result):
    return lambda method, params: result


def _verify(result, **kw):
    used = set()
    args = dict(tx_hash=TXH, network="xrpl:1", min_drops=MIN_DROPS,
                pay_to=PAY_TO, used_set=used,
                rpc=_rpc_ok(result), invoice_salt_val=SALT)
    args.update(kw)
    return xrplmod.verify_xrpl_payment(**args)


# ---------------------------------------------------------------- helpers

def test_valid_r_address():
    assert xrplmod.valid_r_address(PAY_TO)
    assert xrplmod.valid_r_address(PAYER)
    assert not xrplmod.valid_r_address("rBAD!")
    assert not xrplmod.valid_r_address("0x1234")
    assert not xrplmod.valid_r_address("")
    assert not xrplmod.valid_r_address(None)


def test_drops_math_no_floats():
    assert xrplmod.xrp_to_drops_str("1") == "1000000"
    assert xrplmod.xrp_to_drops_str("0.000001") == "1"
    assert xrplmod.drops_to_int("500000") == 500000
    with pytest.raises(ValueError):
        xrplmod.drops_to_int("1.5")
    with pytest.raises(ValueError):
        xrplmod.drops_to_int("abc")


def test_invoice_id_binding():
    inv = xrplmod.invoice_id(PAY_TO, str(MIN_DROPS), "xrpl:1", SALT)
    assert len(inv) == 64
    assert xrplmod.invoice_id_valid(inv, PAY_TO, str(MIN_DROPS),
                                    "xrpl:1", SALT)
    # wrong amount -> invalid
    assert not xrplmod.invoice_id_valid(inv, PAY_TO, str(MIN_DROPS + 1),
                                        "xrpl:1", SALT)
    # wrong payee -> invalid
    assert not xrplmod.invoice_id_valid(inv, OTHER, str(MIN_DROPS),
                                        "xrpl:1", SALT)
    # malformed -> invalid, never raises
    assert not xrplmod.invoice_id_valid("zzz", PAY_TO, str(MIN_DROPS),
                                        "xrpl:1", SALT)


# ---------------------------------------------------------------- verifier

def test_happy_path():
    ok, info = _verify(_tx_result())
    assert ok, info
    assert info["paid_drops"] == MIN_DROPS
    assert info["payer"] == PAYER
    assert info["replay_key"] == "xrpl:1:" + TXH.lower()
    assert info["ledger_index"] == 21224500


def test_mainnet_verifies_when_configured():
    # Mainnet flip: xrpl:0 verifies like testnet when the operator has
    # mainnet RPCs (defaults exist). Replay keys stay network-scoped.
    result = _tx_result(**{"InvoiceID": xrplmod.invoice_id(
        PAY_TO, str(MIN_DROPS), "xrpl:0", SALT)})
    ok, info = _verify(result, network="xrpl:0")
    assert ok, info
    assert info["replay_key"].startswith("xrpl:0:")


def test_rejects_bad_hash():
    ok, info = _verify(_tx_result(), tx_hash="zzz")
    assert not ok and "format" in info["reason"]


def test_rejects_replay():
    used = {("xrpl:1:" + TXH.lower())}
    ok, info = _verify(_tx_result(), used_set=used)
    assert not ok and info["reason"].startswith("replay:")


def test_rejects_unknown_tx():
    ok, info = _verify(None)
    assert not ok and "not found" in info["reason"]


def test_rejects_unvalidated():
    r = _tx_result()
    r["validated"] = False
    ok, info = _verify(r)
    assert not ok and "validated" in info["reason"]


def test_rejects_non_payment():
    ok, info = _verify(_tx_result(TransactionType="EscrowCreate"))
    assert not ok and "not a Payment" in info["reason"]


def test_rejects_wrong_destination():
    ok, info = _verify(_tx_result(Destination=OTHER))
    assert not ok and "destination mismatch" in info["reason"]


def test_rejects_partial_payment_flag():
    ok, info = _verify(_tx_result(Flags=xrplmod.TF_PARTIAL_PAYMENT))
    assert not ok and "tfPartialPayment" in info["reason"]


def test_rejects_underpaid():
    r = _tx_result()
    r["meta"] = {"delivered_amount": str(MIN_DROPS - 1)}
    ok, info = _verify(r)
    assert not ok and "underpaid" in info["reason"]


def test_rejects_iou_asset():
    r = _tx_result()
    r["meta"] = {"delivered_amount": {
        "currency": xrplmod.RLUSD_CURRENCY_HEX,
        "issuer": xrplmod.RLUSD_ISSUER_MAINNET, "value": "1"}}
    ok, info = _verify(r)
    assert not ok and "XRP only" in info["reason"]


def test_rejects_bad_invoice_id():
    ok, info = _verify(_tx_result(InvoiceID="B" * 64))
    assert not ok and "InvoiceID" in info["reason"]


def test_rejects_missing_invoice_id():
    tx = _tx_result()
    del tx["tx"]["InvoiceID"]
    ok, info = _verify(tx)
    assert not ok and "InvoiceID" in info["reason"]


def test_rejects_old_tx():
    tx = _tx_result()
    tx["tx"]["date"] = int(time.time()) - 946684800 - 7200  # 2h ago
    ok, info = _verify(tx, max_age_s=3600)
    assert not ok and "too old" in info["reason"]


# ---------------------------------------------------------------- x402 wiring

def test_parse_x_payment_xrpl():
    import base64, json
    payload = {"x402Version": 2, "scheme": "exact", "network": "xrpl:1",
               "payload": {"txHash": TXH}}
    hdr = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    os.environ["AWL_PAY_TO_XRPL"] = PAY_TO
    try:
        proof, net, psig, err = x402mod.parse_x_payment(hdr)
    finally:
        del os.environ["AWL_PAY_TO_XRPL"]
    assert err is None, err
    assert proof == TXH and net == "xrpl:1" and psig is None


def test_parse_x_payment_xrpl_disabled():
    import base64, json
    payload = {"x402Version": 2, "scheme": "exact", "network": "xrpl:1",
               "payload": {"txHash": TXH}}
    hdr = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    os.environ.pop("AWL_PAY_TO_XRPL", None)
    proof, net, psig, err = x402mod.parse_x_payment(hdr)
    assert err and "not enabled" in err


def test_payment_terms_advertises_xrpl():
    os.environ["AWL_PAY_TO_XRPL"] = PAY_TO
    os.environ["AWL_XRPL_INVOICE_SALT"] = SALT
    try:
        body = x402mod.payment_terms("example.com", 1, oracle=MockOracle(
            {("xrpl", "XRP"): 2.0}))
    finally:
        del os.environ["AWL_PAY_TO_XRPL"]
        del os.environ["AWL_XRPL_INVOICE_SALT"]
    xrpl_terms = [a for a in body["accepts"] if a["network"] == "xrpl:1"]
    assert len(xrpl_terms) == 1
    t = xrpl_terms[0]
    assert t["asset"] == "XRP"
    assert t["payTo"] == PAY_TO
    # 1 cent at $2/XRP = 5000 drops
    assert t["amount"] == "5000"
    assert len(t["extra"]["invoiceId"]) == 64


def test_payment_terms_skips_xrpl_without_price():
    os.environ["AWL_PAY_TO_XRPL"] = PAY_TO
    try:
        body = x402mod.payment_terms("example.com", 1,
                                     oracle=MockOracle({}))
    finally:
        del os.environ["AWL_PAY_TO_XRPL"]
    assert not [a for a in body["accepts"] if a["network"] == "xrpl:1"]


def test_xrpl_min_drops_never_zero():
    oracle = MockOracle({("xrpl", "XRP"): 1_000_000.0})  # absurd price
    assert x402mod.xrpl_min_drops(10_000, oracle) == 1
    assert x402mod.xrpl_min_drops(10_000, MockOracle({})) is None
