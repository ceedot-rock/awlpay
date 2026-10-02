"""awLPay Stellar rail tests: verifier checklist against mocked Horizon data.

Covers (all with a fake RPC — no network):
  (a) pure helpers: StrKey G-address validation, stroops math,
      memo-hash challenge binding
  (b) verify_stellar_payment: happy path (payment op and create_account
      op), then each refusal — 404, unsuccessful tx, immature ledger,
      no payment op, wrong asset, recipient mismatch, underpaid,
      memo mismatch / missing memo_type, replay, bad address

No mainnet, no real funds, no network. The live testnet E2E lives in
tests/manual/stellar_e2e.py (throwaway wallets, run by hand).
"""

from __future__ import annotations

import base64
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

from server import stellar as stmod  # noqa: E402

# Throwaway G-addresses (test only — random keys, never real wallets).
PAY_TO = "GD3ENTDDVQ7UBHQ5GQGQWNQNR2CECWA3FPRY5M5WMGKW7P3IKYKWZE3E"
PAYER = "GBP4LW7ILBTEIIF2FCTYGDNF6ZUG2QV733YVC4DE7RGBASOLQK4ESU42"
OTHER = "GBTRINAILOJM53UO5YEYBPK6OQVEKKTPWJ5BTQ6I6WTQH2D4UZTIWOAW"
TXH = "C" * 64
SALT = "test-salt-only"
MIN_STROOPS = 50_000_000  # 5 XLM
TX_LEDGER = 1000
LATEST_LEDGER = 1010  # 10 deep — final


def _memo_b64():
    mid = stmod.memo_id(PAY_TO, str(MIN_STROOPS), "stellar:testnet", SALT)
    return base64.b64encode(bytes.fromhex(mid)).decode()


def _created_at():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 60))


def _tx():
    return {
        "id": TXH.lower(),
        "hash": TXH.lower(),
        "ledger": TX_LEDGER,
        "successful": True,
        "memo_type": "hash",
        "memo": _memo_b64(),
        "created_at": _created_at(),
    }


def _pay_op(**over):
    op = {
        "type": "payment",
        "asset_type": "native",
        "from": PAYER,
        "to": PAY_TO,
        "amount": "5.0000000",
        "transaction_successful": True,
    }
    op.update(over)
    return op


def _create_account_op(**over):
    op = {
        "type": "create_account",
        "account": PAY_TO,
        "funder": PAYER,
        "starting_balance": "5.0000000",
        "transaction_successful": True,
    }
    op.update(over)
    return op


def _rpc_ok(ops_records):
    def fn(method, params):
        if method == "transaction":
            return _tx()
        if method == "operations":
            return {"_embedded": {"records": ops_records}}
        if method == "latest_ledger":
            return {"_embedded": {"records": [{"sequence": LATEST_LEDGER}]}}
        raise AssertionError("unexpected rpc method %r" % (method,))
    return fn


def _verify(ops_records, **kw):
    used = set()
    args = dict(tx_hash=TXH, network="stellar:testnet",
                min_stroops=MIN_STROOPS, pay_to=PAY_TO, used_set=used,
                rpc=_rpc_ok(ops_records), memo_salt_val=SALT)
    args.update(kw)
    return stmod.verify_stellar_payment(**args)


# ---------------------------------------------------------------- helpers

def test_valid_g_address():
    assert stmod.valid_g_address(PAY_TO)
    assert stmod.valid_g_address(PAYER)
    assert not stmod.valid_g_address("G" + "A" * 55)  # bad checksum
    assert not stmod.valid_g_address("G" + "A" * 54)  # too short
    assert not stmod.valid_g_address("G" + "A" * 56)  # too long
    assert not stmod.valid_g_address("rL9zi7gdzYbXizDP5CgKRMD3jVkMbpMwR9")
    assert not stmod.valid_g_address("G00000000000000000000000000000000000000000000000000000")
    assert not stmod.valid_g_address("")
    assert not stmod.valid_g_address(None)


def test_stroops_math_no_floats():
    assert stmod.xlm_to_stroops_str("1") == "10000000"
    assert stmod.xlm_to_stroops_str("0.0000001") == "1"
    assert stmod.xlm_to_stroops_str("1.5") == "15000000"
    assert stmod.stroops_to_int("50000000") == 50_000_000
    with pytest.raises(ValueError):
        stmod.xlm_to_stroops_str("1.00000001")  # 8 decimals
    with pytest.raises(ValueError):
        stmod.xlm_to_stroops_str("abc")
    with pytest.raises(ValueError):
        stmod.stroops_to_int("1.5")


def test_memo_id_binding():
    mid = stmod.memo_id(PAY_TO, str(MIN_STROOPS), "stellar:testnet", SALT)
    assert len(mid) == 64
    b64 = base64.b64encode(bytes.fromhex(mid)).decode()
    assert stmod.memo_id_valid(b64, PAY_TO, str(MIN_STROOPS),
                               "stellar:testnet", SALT)
    # wrong amount -> invalid
    assert not stmod.memo_id_valid(b64, PAY_TO, str(MIN_STROOPS + 1),
                                   "stellar:testnet", SALT)
    # wrong payee -> invalid
    assert not stmod.memo_id_valid(b64, OTHER, str(MIN_STROOPS),
                                   "stellar:testnet", SALT)
    # malformed -> invalid, never raises
    assert not stmod.memo_id_valid("zzz", PAY_TO, str(MIN_STROOPS),
                                   "stellar:testnet", SALT)
    assert not stmod.memo_id_valid(base64.b64encode(b"short").decode(),
                                   PAY_TO, str(MIN_STROOPS),
                                   "stellar:testnet", SALT)


# ---------------------------------------------------------------- verifier

def test_happy_path_payment():
    ok, info = _verify([_pay_op()])
    assert ok, info
    assert info["paid_stroops"] == MIN_STROOPS
    assert info["payer"] == PAYER
    assert info["op_kind"] == "payment"
    assert info["replay_key"] == "stellar:testnet:" + TXH.lower()
    assert info["ledger"] == TX_LEDGER


def test_happy_path_create_account():
    # A fresh payTo can only be funded via create_account — accepted,
    # documented in _find_pay_op.
    ok, info = _verify([_create_account_op()])
    assert ok, info
    assert info["paid_stroops"] == MIN_STROOPS
    assert info["op_kind"] == "create_account"
    assert info["payer"] == PAYER  # funder


def test_happy_path_create_account_to_field():
    # Some SDKs surface the destination as "to" instead of "account".
    ok, info = _verify([_create_account_op(to=PAY_TO)])
    assert ok, info
    assert info["op_kind"] == "create_account"


def test_pubnet_verifies_like_testnet():
    def fn(method, params):
        if method == "transaction":
            tx = _tx()
            tx["memo"] = base64.b64encode(bytes.fromhex(
                stmod.memo_id(PAY_TO, str(MIN_STROOPS),
                              "stellar:pubnet", SALT))).decode()
            return tx
        return _rpc_ok([_pay_op()])(method, params)
    ok, info = stmod.verify_stellar_payment(
        tx_hash=TXH, network="stellar:pubnet", min_stroops=MIN_STROOPS,
        pay_to=PAY_TO, used_set=set(), rpc=fn, memo_salt_val=SALT)
    assert ok, info
    assert info["replay_key"].startswith("stellar:pubnet:")


def test_rejects_bad_hash():
    ok, info = _verify([_pay_op()], tx_hash="zzz")
    assert not ok and "format" in info["reason"]


def test_rejects_replay():
    used = {"stellar:testnet:" + TXH.lower()}
    ok, info = _verify([_pay_op()], used_set=used)
    assert not ok and info["reason"].startswith("replay:")


def test_rejects_404():
    ok, info = stmod.verify_stellar_payment(
        tx_hash=TXH, network="stellar:testnet", min_stroops=MIN_STROOPS,
        pay_to=PAY_TO, used_set=set(),
        rpc=lambda m, p: None, memo_salt_val=SALT)
    assert not ok and "not found" in info["reason"]


def test_rejects_unsuccessful_tx():
    def fn(method, params):
        if method == "transaction":
            tx = _tx()
            tx["successful"] = False
            return tx
        return _rpc_ok([_pay_op()])(method, params)
    ok, info = stmod.verify_stellar_payment(
        tx_hash=TXH, network="stellar:testnet", min_stroops=MIN_STROOPS,
        pay_to=PAY_TO, used_set=set(), rpc=fn, memo_salt_val=SALT)
    assert not ok and "not successful" in info["reason"]


def test_rejects_immature_ledger():
    def fn(method, params):
        if method == "latest_ledger":
            return {"_embedded": {"records": [{"sequence": TX_LEDGER + 1}]}}
        return _rpc_ok([_pay_op()])(method, params)
    ok, info = stmod.verify_stellar_payment(
        tx_hash=TXH, network="stellar:testnet", min_stroops=MIN_STROOPS,
        pay_to=PAY_TO, used_set=set(), rpc=fn, memo_salt_val=SALT)
    assert not ok and "too fresh" in info["reason"]


def test_rejects_no_payment_op():
    ok, info = _verify([])
    assert not ok and "no qualifying XLM payment op" in info["reason"]


def test_rejects_failed_op():
    ok, info = _verify([_pay_op(transaction_successful=False)])
    assert not ok and "no qualifying XLM payment op" in info["reason"]


def test_rejects_wrong_asset():
    ok, info = _verify([_pay_op(asset_type="credit_alphanum4")])
    assert not ok and "no qualifying XLM payment op" in info["reason"]


def test_rejects_recipient_mismatch():
    ok, info = _verify([_pay_op(to=OTHER)])
    assert not ok and "no qualifying XLM payment op" in info["reason"]


def test_rejects_underpaid():
    ok, info = _verify([_pay_op(amount="4.9999999")])
    assert not ok and "underpaid" in info["reason"]
    assert info["paid_stroops"] == 49_999_999


def test_rejects_memo_mismatch():
    def fn(method, params):
        if method == "transaction":
            tx = _tx()
            tx["memo"] = base64.b64encode(b"\x00" * 32).decode()
            return tx
        return _rpc_ok([_pay_op()])(method, params)
    ok, info = stmod.verify_stellar_payment(
        tx_hash=TXH, network="stellar:testnet", min_stroops=MIN_STROOPS,
        pay_to=PAY_TO, used_set=set(), rpc=fn, memo_salt_val=SALT)
    assert not ok and "memo hash mismatch" in info["reason"]


def test_rejects_missing_memo_type():
    def fn(method, params):
        if method == "transaction":
            tx = _tx()
            tx["memo_type"] = "text"
            return tx
        return _rpc_ok([_pay_op()])(method, params)
    ok, info = stmod.verify_stellar_payment(
        tx_hash=TXH, network="stellar:testnet", min_stroops=MIN_STROOPS,
        pay_to=PAY_TO, used_set=set(), rpc=fn, memo_salt_val=SALT)
    assert not ok and "memo_type" in info["reason"]


def test_rejects_old_tx():
    def fn(method, params):
        if method == "transaction":
            tx = _tx()
            tx["created_at"] = time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 7200))
            return tx
        return _rpc_ok([_pay_op()])(method, params)
    ok, info = stmod.verify_stellar_payment(
        tx_hash=TXH, network="stellar:testnet", min_stroops=MIN_STROOPS,
        pay_to=PAY_TO, used_set=set(), rpc=fn, memo_salt_val=SALT,
        max_age_s=3600)
    assert not ok and "too old" in info["reason"]


def test_rejects_bad_address():
    ok, info = _verify([_pay_op()], pay_to="G" + "A" * 55)
    assert not ok and "bad payTo G-address" in info["reason"]


def test_does_not_mutate_used_set():
    used = set()
    ok, info = _verify([_pay_op()], used_set=used)
    assert ok, info
    assert used == set()  # caller consumes info["replay_key"]
