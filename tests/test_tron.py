"""awLPay Tron rail tests: verifier checklist against mocked chain data.

Covers (all with a fake HTTP API — no network):
  (a) pure helpers: base58check codec (known mainnet USDT pair),
      T-address validation, USDT units math, binding_message parity
      with x402's, TIP-191 digest shape
  (b) verify_tron_sig: roundtrip with a pure-Python test signer,
      wrong-message and wrong-key rejection, Ethereum-prefix
      signatures rejected (by design)
  (c) verify_tron_payment: happy path, then each refusal — tx not
      found, failed receipt, wrong contract, wrong topic count,
      recipient mismatch, underpaid, replay, bad payTo address,
      missing payerSig, bad sig, wrong signer, insufficient
      confirmations

No mainnet, no real funds, no network.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

from server import tron as tronmod  # noqa: E402
from server import ethsig  # noqa: E402
from server import x402 as x402mod  # noqa: E402

# Throwaway addresses (test only — random payloads, never real wallets).
PAY_TO = tronmod.hex_to_t_address("41" + "11" * 20)
PAYER = tronmod.hex_to_t_address("41" + "22" * 20)
OTHER = tronmod.hex_to_t_address("41" + "33" * 20)
USDT20 = tronmod.USDT_CONTRACT_MAINNET[2:]  # 20-byte hex, no 41 prefix
TXH = "ab" * 32
RESOURCE = "/v1/test-resource"
MIN_UNITS = 1_000_000  # 1 USDT


# --------------------------------------------------------------------------
# pure-Python TIP-191 test signer (test-only; the module never signs)
# --------------------------------------------------------------------------

_TEST_PRIV = int("0x" + "7" * 64, 16) % (ethsig._N - 1) + 1


def _test_sign(priv: int, msg: bytes) -> str:
    """Deterministic secp256k1 sign of tron_prefixed_hash(msg).
    Returns 65-byte r||s||v hex (v in 27/28), like TronWeb signMessageV2."""
    d = priv % ethsig._N
    digest = tronmod.tron_prefixed_hash(msg)
    e = int.from_bytes(digest, "big") % ethsig._N
    k = (int.from_bytes(ethsig.keccak256(b"awlpay-tron-test-k"), "big")
         % (ethsig._N - 1)) + 1
    r_pt = ethsig._point_mul(k)
    r = r_pt[0] % ethsig._N
    s = (pow(k, ethsig._N - 2, ethsig._N) * (e + r * d)) % ethsig._N
    assert r != 0 and s != 0
    if s > ethsig._N // 2:  # low-s, like real wallets
        s = ethsig._N - s
    pub = ethsig._point_mul(d)
    for rid in (0, 1):
        if ethsig.ecrecover(digest, rid, r, s) == pub:
            return "%064x%064x%02x" % (r, s, 27 + rid)
    raise AssertionError("no recovery id matched")


def _payer_addr(priv: int) -> str:
    return tronmod.pubkey_point_to_t_address(ethsig._point_mul(priv))


# The test payer is the key that "sends" the mocked USDT.
PAYER_KEY = _TEST_PRIV
PAYER_ADDR = _payer_addr(PAYER_KEY)
PAYER_SIG = _test_sign(PAYER_KEY, tronmod.binding_message(TXH, RESOURCE))


# --------------------------------------------------------------------------
# mocked gettransactioninfobyid
# --------------------------------------------------------------------------

def _topic(addr_hex41: str) -> str:
    """T-address -> 32-byte log topic (12 zero bytes + 20 address bytes)."""
    return "00" * 12 + addr_hex41[2:]


def _tx_info(log=None, receipt_result="SUCCESS", block_number=50_000_000,
             head_number=None):
    if log is None:
        log = {
            "address": USDT20,
            "topics": [
                tronmod.TRANSFER_TOPIC,
                _topic(tronmod.t_address_to_hex(PAYER_ADDR)),
                _topic(tronmod.t_address_to_hex(PAY_TO)),
            ],
            "data": "%064x" % MIN_UNITS,
        }
    resp = {
        "id": TXH,
        "blockNumber": block_number,
        "receipt": {"result": receipt_result},
        "log": [log],
    }
    return resp


def _rpc_for(info_resp, head_number=50_000_100):
    def fake(path, body):
        if path == "/wallet/getnowblock":
            return {"block_header": {"raw_data": {"number": head_number}}}
        return info_resp
    return fake


def _verify(info_resp, **kw):
    args = dict(tx_hash=TXH, network="tron:0", min_units=MIN_UNITS,
                pay_to=PAY_TO, used_set=set(), rpc=_rpc_for(info_resp),
                payer_sig=PAYER_SIG, resource=RESOURCE)
    args.update(kw)
    return tronmod.verify_tron_payment(**args)


# ---------------------------------------------------------------- helpers

def test_b58check_known_pair():
    # Ground truth: mainnet USDT contract (widely published pair).
    assert (tronmod.hex_to_t_address(
        "41a614f803b6fd780986a42c78ec9c7f77e6ded13c")
        == "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t")
    assert (tronmod.t_address_to_hex("TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t")
        == "41a614f803b6fd780986a42c78ec9c7f77e6ded13c")


def test_b58check_roundtrip():
    for addr in (PAY_TO, PAYER, OTHER, "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"):
        assert tronmod.hex_to_t_address(tronmod.t_address_to_hex(addr)) \
            == addr


def test_b58check_bad_checksum():
    bad = PAY_TO[:-1] + ("1" if PAY_TO[-1] != "1" else "2")
    assert not tronmod.valid_t_address(bad)
    assert not tronmod.valid_t_address("TBad!chars")
    assert not tronmod.valid_t_address("")
    assert not tronmod.valid_t_address(None)
    assert not tronmod.valid_t_address(12345)


def test_valid_t_address():
    assert tronmod.valid_t_address(PAY_TO)
    assert tronmod.valid_t_address("TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t")
    # ethereum-style address is not a T-address
    assert not tronmod.valid_t_address(
        "0xa614f803b6fd780986a42c78ec9c7f77e6ded13c")


def test_usdt_units_math_no_floats():
    assert tronmod.usdt_to_units_str("1") == "1000000"
    assert tronmod.usdt_to_units_str("1.5") == "1500000"
    assert tronmod.usdt_to_units_str("0.000001") == "1"
    with pytest.raises(ValueError):
        tronmod.usdt_to_units_str("abc")
    with pytest.raises(ValueError):
        tronmod.usdt_to_units_str("-1")


def test_binding_message_matches_x402():
    # Byte-identical duplicate — x402 must import this module's rail,
    # so the copy (not an import) avoids a circular import.
    assert tronmod.binding_message(TXH, RESOURCE) == \
        x402mod.binding_message(TXH, RESOURCE)


def test_tron_prefix_differs_from_eip191_by_design():
    msg = b"hello"
    tron_digest = tronmod.tron_prefixed_hash(msg)
    eth_digest = ethsig.eth_personal_message(msg)
    assert tron_digest != eth_digest
    assert tronmod.tron_prefixed_hash(msg) == ethsig.keccak256(
        b"\x19TRON Signed Message:\n" + b"5" + msg)


# ---------------------------------------------------------------- sig

def test_verify_tron_sig_roundtrip():
    msg = tronmod.binding_message(TXH, RESOURCE)
    assert tronmod.verify_tron_sig(msg, PAYER_SIG) == PAYER_ADDR


def test_verify_tron_sig_accepts_0x_and_rejects_64byte():
    msg = tronmod.binding_message(TXH, RESOURCE)
    assert tronmod.verify_tron_sig(msg, "0x" + PAYER_SIG) == PAYER_ADDR
    # 64-byte (v-less) signatures are refused: without v the
    # recovered key is ambiguous, so they cannot bind safely.
    assert tronmod.verify_tron_sig(msg, PAYER_SIG[:128]) is None


def test_verify_tron_sig_wrong_message():
    msg = tronmod.binding_message(TXH, RESOURCE)
    sig = _test_sign(PAYER_KEY, b"different message")
    assert tronmod.verify_tron_sig(msg, sig) != PAYER_ADDR


def test_verify_tron_sig_ethereum_prefix_rejected_by_design():
    # Same key, same message, but signed under the EIP-191 Ethereum
    # prefix: must NOT verify as a Tron signature.
    msg = tronmod.binding_message(TXH, RESOURCE)
    eth_sig = ethsig  # EIP-191 signer built from ethsig primitives
    digest = eth_sig.eth_personal_message(msg)
    e = int.from_bytes(digest, "big") % eth_sig._N
    k = (int.from_bytes(eth_sig.keccak256(b"awlpay-eth-test-k"), "big")
         % (eth_sig._N - 1)) + 1
    r = eth_sig._point_mul(k)[0] % eth_sig._N
    s = (pow(k, eth_sig._N - 2, eth_sig._N)
         * (e + r * PAYER_KEY)) % eth_sig._N
    if s > eth_sig._N // 2:
        s = eth_sig._N - s
    sig_hex = "%064x%064x%02x" % (r, s, 27)
    assert tronmod.verify_tron_sig(msg, sig_hex) != PAYER_ADDR


def test_verify_tron_sig_malformed():
    msg = tronmod.binding_message(TXH, RESOURCE)
    assert tronmod.verify_tron_sig(msg, "zz") is None
    assert tronmod.verify_tron_sig(msg, "00" * 65) is None
    assert tronmod.verify_tron_sig(msg, None) is None
    assert tronmod.verify_tron_sig(msg, "") is None


# ---------------------------------------------------------------- verifier

def test_happy_path():
    ok, info = _verify(_tx_info())
    assert ok, info
    assert info["paid_units"] == MIN_UNITS
    assert info["payer"] == PAYER_ADDR
    assert info["replay_key"] == "tron:0:" + TXH
    assert info["block_number"] == 50_000_000


def test_rejects_unknown_tx():
    ok, info = _verify({})
    assert not ok and "not found" in info["reason"]
    ok, info = _verify(None)
    assert not ok and "not found" in info["reason"]


def test_rejects_failed_receipt():
    ok, info = _verify(_tx_info(receipt_result="REVERT"))
    assert not ok and "failed" in info["reason"]


def test_rejects_wrong_contract():
    log = _tx_info()["log"][0]
    log["address"] = "00" * 20  # some other contract
    ok, info = _verify(_tx_info(log=log))
    assert not ok and "no matching USDT Transfer" in info["reason"]


def test_rejects_wrong_topic_count():
    log = _tx_info()["log"][0]
    log["topics"] = log["topics"][:2]
    ok, info = _verify(_tx_info(log=log))
    assert not ok and "no matching USDT Transfer" in info["reason"]


def test_rejects_wrong_event_topic():
    log = _tx_info()["log"][0]
    log["topics"][0] = "00" * 32
    ok, info = _verify(_tx_info(log=log))
    assert not ok and "no matching USDT Transfer" in info["reason"]


def test_rejects_recipient_mismatch():
    log = _tx_info()["log"][0]
    log["topics"][2] = _topic(tronmod.t_address_to_hex(OTHER))
    ok, info = _verify(_tx_info(log=log))
    assert not ok and "no matching USDT Transfer" in info["reason"]


def test_rejects_underpaid():
    log = _tx_info()["log"][0]
    log["data"] = "%064x" % (MIN_UNITS - 1)
    ok, info = _verify(_tx_info(log=log))
    assert not ok and "underpaid" in info["reason"]
    assert info["paid_units"] == MIN_UNITS - 1


def test_rejects_replay():
    used = {"tron:0:" + TXH}
    ok, info = _verify(_tx_info(), used_set=used)
    assert not ok and info["reason"].startswith("replay:")


def test_replay_keys_are_network_scoped():
    used = {"tron:1:" + TXH}  # nile, not mainnet
    ok, info = _verify(_tx_info(), used_set=used)
    assert ok, info


def test_rejects_bad_payto_address():
    ok, info = _verify(_tx_info(), pay_to="0xbad")
    assert not ok and "payTo" in info["reason"]


def test_rejects_bad_tx_hash():
    ok, info = _verify(_tx_info(), tx_hash="zzz")
    assert not ok and "format" in info["reason"]


def test_rejects_unsupported_network():
    ok, info = _verify(_tx_info(), network="eth:0")
    assert not ok and "unsupported network" in info["reason"]


def test_rejects_missing_payer_sig():
    ok, info = _verify(_tx_info(), payer_sig=None)
    assert not ok and "missing payerSig" in info["reason"]


def test_rejects_bad_sig():
    ok, info = _verify(_tx_info(), payer_sig="00" * 65)
    assert not ok and "payerSig invalid" in info["reason"]


def test_rejects_wrong_signer():
    # Valid Tron signature, but from a different key than the USDT sender.
    other_key = (_TEST_PRIV * 3) % (ethsig._N - 1) + 1
    sig = _test_sign(other_key, tronmod.binding_message(TXH, RESOURCE))
    ok, info = _verify(_tx_info(), payer_sig=sig)
    assert not ok and "signer mismatch" in info["reason"]


def test_rejects_sig_for_other_resource():
    # Signature bound to a different endpoint: replay at another
    # resource is refused.
    sig = _test_sign(PAYER_KEY, tronmod.binding_message(TXH, "/other"))
    ok, info = _verify(_tx_info(), payer_sig=sig)
    assert not ok and "signer mismatch" in info["reason"]


def test_confirmations_default_zero_ok():
    # Default: SUCCESS receipt is enough for small x402 payments.
    ok, info = _verify(_tx_info(block_number=50_000_100))
    assert ok, info


def test_confirmations_enforced_when_demanded():
    ok, info = _verify(_tx_info(block_number=50_000_100),
                       min_confirmations=20)
    assert not ok and "insufficient confirmations" in info["reason"]
    # head 50_000_100 - block 50_000_080 = 20 -> passes
    ok, info = _verify(_tx_info(block_number=50_000_080),
                       min_confirmations=20)
    assert ok, info


def test_payment_terms_disabled_without_env(monkeypatch):
    monkeypatch.delenv("AWL_PAY_TO_TRON", raising=False)
    assert tronmod.tron_payment_terms() is None


def test_payment_terms_advertises_rail(monkeypatch):
    monkeypatch.setenv("AWL_PAY_TO_TRON", PAY_TO)
    terms = tronmod.tron_payment_terms()
    assert terms is not None
    assert terms["asset"] == "USDT"
    assert terms["network"] == "tron:0"
    assert terms["payTo"] == PAY_TO
    assert terms["extra"]["contract"] == tronmod.USDT_CONTRACT_MAINNET


def test_does_not_mutate_used_set():
    used = set()
    ok, info = _verify(_tx_info(), used_set=used)
    assert ok, info
    assert used == set()
