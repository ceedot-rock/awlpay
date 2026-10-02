"""awLPay BSC rail tests: BNB Smart Chain USDT (BEP-20) verification.

The BSC rail reuses the generic EVM verification path (_verify_evm) with
a per-rail token config: USDT BEP-20, 18 decimals. These tests pin:

  * rail config: token symbol, decimals, per-cent unit math
  * verify_payment on eip155:56 with a mocked receipt (valid, underpaid,
    wrong-token, replay)
  * payment_terms advertises the BSC accepts[] entry with 18-decimal amount

Mock receipts follow the tests/test_x402.py pattern: canned
eth_getTransactionReceipt responses; payer binding signatures are REAL
EIP-191 signatures from throwaway test keys.
"""

from __future__ import annotations

import base64
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

from server import x402 as x402mod  # noqa: E402
from server import ethsig as ethsigmod  # noqa: E402
from server import fees as feesmod  # noqa: E402

BSC_MAINNET = "eip155:56"
USDT_BSC = "0x55d398326f99059fF775485246999027B3197955"
TRANSFER_TOPIC = x402mod.TRANSFER_TOPIC
PAY_TO = "0xBAd41cF0f0d5442f9A53630F8081BFd257DA019b"
ONE_CENT_UNITS = 10 ** 16  # 1 cent of 18-decimal USDT


def _topic_addr(a: str) -> str:
    return "0x" + "00" * 12 + a[2:].lower()


def _transfer_log(token: str, frm: str, to: str, value: int) -> dict:
    return {"address": token,
            "topics": [TRANSFER_TOPIC, _topic_addr(frm), _topic_addr(to)],
            "data": hex(value)}


def _receipt(token, frm, to, value, tx_hash):
    return {"status": "0x1", "from": frm,
            "logs": [_transfer_log(token, frm, to, value)]}


def sign_eoa(msg: bytes, priv: int):
    """Deterministic test-only ECDSA signer (mirrors test_x402.py)."""
    digest = ethsigmod.eth_personal_message(msg)
    z = int.from_bytes(digest, "big")
    k = 0x2A2A2A2A
    rx, ry = ethsigmod._point_mul(k)
    r = rx % ethsigmod._N
    s = (pow(k, ethsigmod._N - 2, ethsigmod._N) * (z + r * priv)) % ethsigmod._N
    parity = ry & 1
    if s > ethsigmod._N // 2:
        s, parity = ethsigmod._N - s, parity ^ 1
    v = 27 + parity
    qx, qy = ethsigmod._point_mul(priv)
    addr = ethsigmod.pubkey_to_address((qx, qy))
    sig = r.to_bytes(32, "big") + s.to_bytes(32, "big") + bytes([v])
    return "0x" + addr.hex(), "0x" + sig.hex()


def make_rpc(receipts):
    def call(method, params):
        if method == "eth_getTransactionReceipt":
            return {"result": receipts.get((params[0] or "").lower())}
        if method == "eth_getCode":
            return {"result": "0x"}
        raise AssertionError("unexpected rpc method: " + method)
    return call


@pytest.fixture()
def bsc_rpc(monkeypatch):
    monkeypatch.setenv("AWL_RPC_BSC", "https://mock-bsc-rpc.invalid")
    monkeypatch.setenv("AWL_USDC_BSC", USDT_BSC)
    yield
    x402mod.clear_test_rpc()


# ---------------------------------------------------------------- config

def test_bsc_rail_config():
    rail = x402mod.EVM_RAILS[BSC_MAINNET]
    assert rail["label"] == "BNB Smart Chain"
    assert rail["testnet"] is False
    assert x402mod.rail_token(BSC_MAINNET) == "USDT"
    assert x402mod.rail_decimals(BSC_MAINNET) == 18
    assert x402mod.units_per_cent_for(BSC_MAINNET) == 10 ** 16
    assert x402mod.min_units_for(BSC_MAINNET, 1) == ONE_CENT_UNITS
    # Existing rails keep 6-decimal math.
    assert x402mod.min_units_for("eip155:8453", 1) == 10_000
    assert x402mod.rail_token("eip155:8453") == "USDC"


def test_bsc_in_configured_rails(bsc_rpc):
    nets = [n for n, _ in x402mod.configured_rails()]
    assert BSC_MAINNET in nets


# ---------------------------------------------------------------- verify

def _verify(tx_hash, amount, token=USDT_BSC, payer_priv=0xB5C,
            min_units=ONE_CENT_UNITS, used=None):
    payer_addr, sig = sign_eoa(
        x402mod.binding_message(tx_hash, "https://x.test/api/pay/execute"),
        payer_priv)
    receipts = {tx_hash.lower(): _receipt(token, payer_addr, PAY_TO, amount,
                                          tx_hash)}
    x402mod.set_test_rpc(make_rpc(receipts))
    try:
        return x402mod.verify_payment(
            tx_hash, BSC_MAINNET, min_units, PAY_TO,
            used if used is not None else set(),
            rpc=x402mod.get_rpc(), payer_sig=sig,
            resource="https://x.test/api/pay/execute")
    finally:
        x402mod.clear_test_rpc()


def test_bsc_valid_payment_verifies(bsc_rpc):
    tx = "0x" + "ab" * 32
    ok, info = _verify(tx, ONE_CENT_UNITS)
    assert ok, info
    assert info["paid_units"] == ONE_CENT_UNITS
    assert info["network"] == BSC_MAINNET
    assert info["replay_key"] == "eip155:56:" + tx.lower()


def test_bsc_underpaid_names_usdt(bsc_rpc):
    tx = "0x" + "cd" * 32
    ok, info = _verify(tx, ONE_CENT_UNITS - 1)
    assert not ok
    assert "underpaid" in info["reason"]
    assert "USDT" in info["reason"], info["reason"]
    assert "USDC" not in info["reason"], info["reason"]


def test_bsc_wrong_token_refused(bsc_rpc):
    tx = "0x" + "ef" * 32
    ok, info = _verify(tx, ONE_CENT_UNITS, token=USDC_BASE_OTHER)
    assert not ok
    assert "no USDT transfer" in info["reason"], info["reason"]


USDC_BASE_OTHER = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"  # noqa: E402


def test_bsc_replay_refused(bsc_rpc):
    tx = "0x" + "12" * 32
    used = set()
    ok, info = _verify(tx, ONE_CENT_UNITS, used=used)
    assert ok, info
    used.add(info["replay_key"])
    ok2, info2 = _verify(tx, ONE_CENT_UNITS, used=used)
    assert not ok2
    assert info2["reason"].startswith("replay:")


# ---------------------------------------------------------------- terms

def test_payment_terms_advertises_bsc(bsc_rpc, monkeypatch):
    monkeypatch.setenv("AWL_PAY_TO", PAY_TO)
    terms = x402mod.payment_terms("x.test", 1)
    entries = [a for a in terms["accepts"] if a["network"] == BSC_MAINNET]
    assert len(entries) == 1
    e = entries[0]
    assert e["amount"] == str(ONE_CENT_UNITS)  # 18-decimal, not 10_000
    assert e["asset"] == USDT_BSC
    assert e["payTo"] == PAY_TO
    assert "USDT" in e["description"]
