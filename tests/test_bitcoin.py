"""awLPay Bitcoin rail tests: verifier checklist against mocked Esplora data.

Covers (all with a fake Esplora — no network):
  (a) pure helpers: bech32/bech32m vectors (BIP-173/350), base58check,
      network-aware address validation, txid/sats math, binding_message
      byte-equality with x402.py
  (b) verify_btc_message: hardcoded embit-signed cross-implementation
      vector (deterministic RFC6979), tamper/wrong-address/wrong-network
      refusals, plus a dynamic embit roundtrip when embit is installed
  (c) verify_btc_payment: confirmed happy path, 0-conf happy path
      (non-RBF + >= 1 sat/vB), then each refusal — 404, underpaid,
      RBF-signaling 0-conf, low-fee 0-conf, replay, bad payTo address,
      wrong-network payTo, missing payerSig, bad payerSig, signature
      from a non-input address, block-above-tip

No mainnet, no real funds, no network. The live signet E2E lives in
tests/manual/ (throwaway wallets, run by hand).
"""

from __future__ import annotations

import base64
import hashlib
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

from server import bitcoin as btcmod  # noqa: E402
from server import x402 as x402mod  # noqa: E402

NET = btcmod.BTC_SIGNET
MAINNET = btcmod.BTC_MAINNET

# Throwaway addresses (test only — never real wallets).
PAY_TO = "tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx"  # tb1 P2WPKH (embit-encoded, checksum-verified)
OTHER = "tb1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcc"  # made-up tb1
# embit-signed fixture (deterministic: sha256("awlpay-bitcoin-test-key-1")
# as key, RFC6979; message = binding_message("c"*64, "example.com")).
SIGNER_ADDR = "motG2vYCMVG2UnPEEaGzpj6eQYKcyRnnti"
SIGNER_SIG = ("IBPwoOTcaSlB7N8nAkYK+/tQgdfsgsa2Z/cwqGn790QObuls8y9/IXQLNhr5kw"
              "Nhx+usPN8s6QJ8BIzTjXbhOrY=")
# A second valid signet P2PKH (deterministic key 2) that is NOT a tx input.
STRANGER_ADDR = "mudZbgHXxeRWmxBmhsBupqFaDqofzMkwob"

TXH = "c" * 64  # == the txid SIGNER_SIG's message binds to
SIGNER_RESOURCE = "example.com"
MIN_SATS = 50_000
TIP_HEIGHT = 900_005
BLOCK_HEIGHT = 900_000


def _vin(addr=SIGNER_ADDR, sequence=0xFFFFFFFD, value=100_000):
    return {"txid": "a" * 64, "vout": 0, "sequence": sequence,
            "prevout": {"scriptpubkey_address": addr, "value": value},
            "scriptsig": "", "witness": []}


def _tx_result(confirmed=True, **over):
    tx = {
        "txid": TXH,
        "fee": 240,
        "vsize": 140,
        "status": {"confirmed": confirmed,
                   "block_height": BLOCK_HEIGHT if confirmed else None,
                   "block_hash": "00" * 32 if confirmed else None,
                   "block_time": int(time.time()) if confirmed else None},
        "vin": [_vin()],
        "vout": [{"scriptpubkey_address": PAY_TO, "value": MIN_SATS,
                  "scriptpubkey": "0014751e76e8199196d454941c45d1b3a323f1433bd6"}],
    }
    tx.update(over)
    return tx


class _Esplora:
    """Fake Esplora: path -> parsed JSON (or None for 404)."""

    def __init__(self, tx=None, tip=TIP_HEIGHT):
        self._tx = tx
        self._tip = tip

    def __call__(self, path):
        if path == "blocks/tip/height":
            return self._tip
        if path == "tx/" + TXH:
            return self._tx  # None -> 404 -> "tx not found"
        return None


_MISSING = object()


def _verify(tx=_MISSING, **kw):
    args = dict(tx_hash=TXH, network=NET, min_sats=MIN_SATS,
                pay_to=PAY_TO, used_set=set(),
                rpc=_Esplora(_tx_result() if tx is _MISSING else tx),
                payer_sig=SIGNER_SIG, resource=SIGNER_RESOURCE)
    args.update(kw)
    return btcmod.verify_btc_payment(**args)


# ---------------------------------------------------------------- helpers

def test_network_ids():
    assert btcmod.BTC_MAINNET == "bip122:000000000019d6689c085ae165831e934"
    assert btcmod.BTC_SIGNET == "bip122:00000008819873e925422c1ff0f99f7cc9bbb"


def test_txid_helpers():
    assert btcmod.valid_txid("ab" * 32)
    assert btcmod.valid_txid("AB" * 32)
    assert btcmod.norm_txid("AB" * 32) == "ab" * 32
    assert not btcmod.valid_txid("zz" * 32)
    assert not btcmod.valid_txid("0x" + "ab" * 32)
    assert not btcmod.valid_txid(None)
    with pytest.raises(ValueError):
        btcmod.norm_txid("short")


def test_sats_math_no_floats():
    assert btcmod.sats_to_int("50000") == 50000
    assert btcmod.btc_to_sats_str("0.00000001") == "1"
    assert btcmod.btc_to_sats_str("1") == "100000000"
    with pytest.raises(ValueError):
        btcmod.sats_to_int("1.5")
    with pytest.raises(ValueError):
        btcmod.sats_to_int("abc")


def test_binding_matches_x402():
    # The 5-line duplicate must stay byte-identical to x402.py's.
    assert btcmod.binding_message(TXH, SIGNER_RESOURCE) == \
        x402mod.binding_message(TXH, SIGNER_RESOURCE)
    msg = btcmod.binding_message("AB" * 32, "https://x.example/pay")
    assert msg == (b"awlpay payment proof\n"
                   b"txHash: " + b"ab" * 32 + b"\n"
                   b"resource: https://x.example/pay")


def test_bech32_vectors():
    # BIP-173 valid (bech32) — decode succeeds, kind tagged.
    for v in ["A12UEL5L", "a12uel5l",
              "an83characterlonghumanreadablepartthatcontainsthenumber1"
              "andtheexcludedcharactersbio1tt5tgs",
              "11qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqc8247j",
              "split1checkupstagehandshakeupstreamerranterredcaperred2y9e3w",
              "?1ezyfcl"]:
        d = btcmod.bech32_decode(v)
        assert d is not None and d[3] == "bech32", v
    # BIP-350 valid (bech32m).
    for v in ["A1LQFN3A", "a1lqfn3a",
              "abcdef1l7aum6echk45nj3s0wdvt2fg8x9yrzpqzd3ryx",
              "split1checkupstagehandshakeupstreamerranterredcaperredlc445v",
              "?1v759aa"]:
        d = btcmod.bech32_decode(v)
        assert d is not None and d[3] == "bech32m", v
    # invalid from both BIPs
    for v in [" 1nwldj5", "A12UEML5L", "a12ueml5l", "1pzry9x0s0muk",
              "x1b4n0q5v", "li1dgmt3", "A1G7SGD8", "10a06t8", "1qzzfhee",
              "notbech32", "", None, 123]:
        assert btcmod.bech32_decode(v) is None, v


def test_address_validation():
    # mainnet
    assert btcmod.valid_btc_address(
        "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4", MAINNET)  # P2WPKH
    assert btcmod.valid_btc_address(
        "bc1p0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7vqzk5jj0",
        MAINNET)  # P2TR (bech32m)
    assert btcmod.valid_btc_address("1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa",
                                    MAINNET)  # P2PKH
    assert btcmod.valid_btc_address("3J98t1WpEZ73CNmQviecrnyiWrnqRhWNLy",
                                    MAINNET)  # P2SH
    # uppercase bech32 is legal (BIP-173); mixed case is not
    assert btcmod.valid_btc_address(
        "BC1QW508D6QEJXTDG4Y5R3ZARVARY0C5XW7KV8F3T4", MAINNET)
    assert not btcmod.valid_btc_address(
        "Bc1QW508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4", MAINNET)
    # signet
    assert btcmod.valid_btc_address(PAY_TO, NET)
    assert btcmod.valid_btc_address(SIGNER_ADDR, NET)  # b58 P2PKH 0x6f
    # wrong network
    assert not btcmod.valid_btc_address(
        "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4", NET)
    assert not btcmod.valid_btc_address(PAY_TO, MAINNET)
    assert not btcmod.valid_btc_address("1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa",
                                        NET)
    # bad checksum / malformed
    assert not btcmod.valid_btc_address(
        "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t5", MAINNET)
    assert not btcmod.valid_btc_address("1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNb",
                                        MAINNET)
    assert not btcmod.valid_btc_address("notanaddress", MAINNET)
    assert not btcmod.valid_btc_address("", NET)
    assert not btcmod.valid_btc_address(None, NET)
    assert not btcmod.valid_btc_address(PAY_TO, "bip122:nope")
    # v0 program with bech32m checksum is invalid (BIP-350)
    assert not btcmod.valid_btc_address(
        "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kg3g4ty", MAINNET)
    # taproot on the wrong hrp
    assert not btcmod.valid_btc_address(
        "bc1p0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7vqzk5jj0",
        NET)


def test_base58check_roundtrip():
    import os as _os
    for ver in (0x00, 0x6F):
        for _ in range(10):
            payload = bytes([ver]) + _os.urandom(20)
            addr = btcmod.base58check_encode(payload)
            assert btcmod.base58check_decode(addr) == payload
    assert btcmod.base58check_decode("1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa") \
        == bytes.fromhex("00" + "62e907b15cbf27d5425399ebf6f0fb50ebb88f18")
    assert btcmod.base58check_decode("1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNb") \
        is None  # bad checksum


def test_ripemd160_fallback_matches_hashlib():
    msgs = [b"", b"a", b"abc", b"message digest",
            b"abcdefghijklmnopqrstuvwxyz", b"x" * 55, b"y" * 56,
            b"z" * 64, b"w" * 1000]
    for m in msgs:
        assert btcmod._ripemd160_pure(m) == hashlib.new("ripemd160", m).digest()
    # spec vectors (verified against hashlib above)
    assert btcmod._ripemd160_pure(b"").hex() == \
        "9c1185a5c5e9fc54612808977ee8f548b2258d31"


# ---------------------------------------------------------------- message sig

def test_verify_btc_message_hardcoded_vector():
    msg = btcmod.binding_message(TXH, SIGNER_RESOURCE)
    assert btcmod.verify_btc_message(SIGNER_ADDR, SIGNER_SIG, msg, NET)
    # tampered message
    assert not btcmod.verify_btc_message(
        SIGNER_ADDR, SIGNER_SIG, btcmod.binding_message("d" * 64,
                                                       SIGNER_RESOURCE), NET)
    # wrong address
    assert not btcmod.verify_btc_message(
        "mjSk1Ny9spzFzYP6vppRirb1QTU8H9rxR3", SIGNER_SIG, msg, NET)
    # wrong network (same string is invalid on mainnet -> False)
    assert not btcmod.verify_btc_message(SIGNER_ADDR, SIGNER_SIG, msg,
                                         MAINNET)
    # malformed signatures
    assert not btcmod.verify_btc_message(SIGNER_ADDR, "!!!", msg, NET)
    assert not btcmod.verify_btc_message(
        SIGNER_ADDR, base64.b64encode(b"x" * 64).decode(), msg, NET)
    assert not btcmod.verify_btc_message(
        SIGNER_ADDR, base64.b64encode(b"\x1b" + b"\x00" * 64).decode(),
        msg, NET)  # header 27-2 < 27
    assert not btcmod.verify_btc_message(SIGNER_ADDR, SIGNER_SIG, "str", NET)
    assert not btcmod.verify_btc_message(SIGNER_ADDR, SIGNER_SIG, msg, "nope")


def test_verify_btc_message_dynamic_embit():
    ec = pytest.importorskip("embit.ec",
                             reason="embit not installed (test-only dep)")
    key = ec.PrivateKey(hashlib.sha256(b"dynamic-test-key").digest())
    pub = key.get_public_key()
    addr = btcmod.base58check_encode(bytes([0x6F]) +
                                     btcmod.hash160(pub.sec()))
    msg = btcmod.binding_message("e" * 64, "https://dyn.example/")
    digest = btcmod.btc_message_hash(msg)
    der = key.sign(digest).serialize()
    assert der[0] == 0x30 and der[2] == 0x02
    lr = der[3]
    r = int.from_bytes(der[4:4 + lr], "big")
    ls = der[5 + lr]
    s = int.from_bytes(der[6 + lr:6 + lr + ls], "big")
    from server import ethsig as ethsigmod
    sig_b64 = None
    for recid in range(4):
        q = ethsigmod.ecrecover(digest, recid, r, s)
        if q is None:
            continue
        for compressed in (True, False):
            if compressed:
                pb = (b"\x02" if q[1] % 2 == 0 else b"\x03") + \
                    q[0].to_bytes(32, "big")
            else:
                pb = b"\x04" + q[0].to_bytes(32, "big") + \
                    q[1].to_bytes(32, "big")
            if btcmod.base58check_encode(bytes([0x6F]) +
                                         btcmod.hash160(pb)) == addr:
                header = 27 + recid + (4 if compressed else 0)
                raw = (bytes([header]) + r.to_bytes(32, "big")
                       + s.to_bytes(32, "big"))
                sig_b64 = base64.b64encode(raw).decode()
    assert sig_b64 is not None
    assert btcmod.verify_btc_message(addr, sig_b64, msg, NET)


# ---------------------------------------------------------------- verifier

def test_confirmed_happy_path():
    ok, info = _verify(_tx_result())
    assert ok, info
    assert info["paid_sats"] == MIN_SATS
    assert info["signer"] == SIGNER_ADDR
    assert info["confirmations"] == TIP_HEIGHT - BLOCK_HEIGHT + 1
    assert info["replay_key"] == NET + ":" + TXH
    assert info["network"] == NET


def test_vout_sum_across_outputs():
    tx = _tx_result()
    tx["vout"] = [
        {"scriptpubkey_address": PAY_TO, "value": MIN_SATS - 100,
         "scriptpubkey": "00"},
        {"scriptpubkey_address": OTHER, "value": 999_999,
         "scriptpubkey": "00"},
        {"scriptpubkey_address": PAY_TO, "value": 100, "scriptpubkey": "00"},
    ]
    ok, info = _verify(tx)
    assert ok, info
    assert info["paid_sats"] == MIN_SATS


def test_zero_conf_happy_path():
    tx = _tx_result(confirmed=False, vin=[_vin(sequence=0xFFFFFFFF)],
                    fee=140, vsize=140)  # exactly 1 sat/vB
    ok, info = _verify(tx)
    assert ok, info
    assert info["confirmations"] == 0


def test_rejects_bad_txid():
    ok, info = _verify(_tx_result(), tx_hash="zzz")
    assert not ok and "format" in info["reason"]


def test_rejects_unsupported_network():
    ok, info = _verify(_tx_result(), network="bip122:nope")
    assert not ok and "unsupported network" in info["reason"]


def test_rejects_replay():
    ok, info = _verify(_tx_result(), used_set={NET + ":" + TXH})
    assert not ok and info["reason"].startswith("replay:")


def test_rejects_404():
    ok, info = _verify(None)  # fake Esplora 404s
    assert not ok and "not found" in info["reason"]


def test_rejects_underpaid():
    tx = _tx_result()
    tx["vout"][0]["value"] = MIN_SATS - 1
    ok, info = _verify(tx)
    assert not ok and "underpaid" in info["reason"]
    assert info["paid_sats"] == MIN_SATS - 1


def test_rejects_bad_payto():
    ok, info = _verify(_tx_result(), pay_to="bc1qw508corrupt")
    assert not ok and "bad payTo address" in info["reason"]


def test_rejects_wrong_network_payto():
    ok, info = _verify(
        _tx_result(), pay_to="bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4")
    assert not ok and "bad payTo address" in info["reason"]


def test_rejects_missing_payer_sig():
    ok, info = _verify(_tx_result(), payer_sig=None)
    assert not ok and "missing payerSig" in info["reason"]


def test_rejects_bad_payer_sig():
    ok, info = _verify(_tx_result(), payer_sig="!!!not-base64!!!")
    assert not ok and "payerSig does not verify" in info["reason"]


def test_rejects_sig_from_non_input_address():
    # SIGNER_SIG is valid and binds this txid, but the recovered signer
    # is not among the tx's input addresses -> refuse.
    tx = _tx_result(vin=[_vin(addr=STRANGER_ADDR)])
    ok, info = _verify(tx)
    assert not ok and "payerSig does not verify" in info["reason"]


def test_rejects_sig_for_wrong_txid():
    # same vin, but the proof claims a different txid -> the bound message
    # digest differs -> the signature cannot verify.
    tx = _tx_result()
    rpc = lambda path: tx if path == "tx/" + "d" * 64 else TIP_HEIGHT
    ok, info = btcmod.verify_btc_payment(
        "d" * 64, NET, MIN_SATS, PAY_TO, set(), rpc=rpc,
        payer_sig=SIGNER_SIG, resource=SIGNER_RESOURCE)
    assert not ok and "payerSig does not verify" in info["reason"]


def test_rejects_rbf_signaling_zero_conf():
    tx = _tx_result(confirmed=False, vin=[_vin(sequence=0xFFFFFFFD)])
    ok, info = _verify(tx)
    assert not ok and "RBF-signaling" in info["reason"]


def test_rejects_missing_sequence_zero_conf():
    vin = _vin()
    del vin["sequence"]
    tx = _tx_result(confirmed=False, vin=[vin])
    ok, info = _verify(tx)
    assert not ok and "RBF-signaling" in info["reason"]


def test_rejects_low_fee_zero_conf():
    tx = _tx_result(confirmed=False, vin=[_vin(sequence=0xFFFFFFFF)],
                    fee=139, vsize=140)  # < 1 sat/vB
    ok, info = _verify(tx)
    assert not ok and "fee too low" in info["reason"]


def test_zero_conf_vsize_from_weight():
    # Blockstream's Esplora omits "vsize": derive ceil(weight/4).
    tx = _tx_result(confirmed=False, vin=[_vin(sequence=0xFFFFFFFF)],
                    fee=140)
    del tx["vsize"]
    tx["weight"] = 560  # -> vsize 140, exactly 1 sat/vB
    ok, info = _verify(tx)
    assert ok, info
    tx["weight"] = 561  # -> vsize 141, fee 140 < 141
    ok, info = _verify(tx)
    assert not ok and "fee too low" in info["reason"]


def test_zero_conf_vsize_from_size_fallback():
    tx = _tx_result(confirmed=False, vin=[_vin(sequence=0xFFFFFFFF)],
                    fee=140)
    del tx["vsize"]
    tx["size"] = 140  # conservative stand-in for vsize
    ok, info = _verify(tx)
    assert ok, info


def test_rejects_zero_conf_without_fee_info():
    tx = _tx_result(confirmed=False, vin=[_vin(sequence=0xFFFFFFFF)])
    del tx["fee"]
    del tx["vsize"]
    ok, info = _verify(tx)
    assert not ok and "no usable fee/vsize" in info["reason"]


def test_rejects_block_above_tip():
    rpc = _Esplora(_tx_result(), tip=BLOCK_HEIGHT - 1)
    ok, info = _verify(rpc=rpc)
    assert not ok and "above the tip" in info["reason"]


def test_rejects_confirmed_without_block_height():
    tx = _tx_result()
    del tx["status"]["block_height"]
    ok, info = _verify(tx)
    assert not ok and "block_height" in info["reason"]


def test_rejects_min_sats_not_positive():
    ok, info = _verify(_tx_result(), min_sats=0)
    assert not ok and "positive" in info["reason"]


# ---------------------------------------------------------------- test seam

def test_set_clear_test_rpc_seam():
    fake = {"txid": TXH}
    btcmod.set_test_rpc(lambda path: fake if path == "tx/" + TXH else 42)
    try:
        assert btcmod.fetch_tx(TXH) is fake
        assert btcmod.fetch_tip_height() == 42
    finally:
        btcmod.clear_test_rpc()
    assert btcmod.get_test_rpc() is None


def test_env_config():
    os.environ["AWL_PAY_TO_BTC"] = " " + PAY_TO + " "
    os.environ["AWL_RPC_BTC"] = "https://a.example, https://b.example"
    os.environ["AWL_RPC_BTC_SIGNET"] = "https://s.example"
    try:
        assert btcmod.btc_pay_to() == PAY_TO
        assert btcmod.rpc_urls_for(MAINNET) == \
            ["https://a.example", "https://b.example", btcmod.FALLBACK_RPC]
        assert btcmod.rpc_urls_for(NET) == ["https://s.example"]
        assert btcmod.rpc_urls_for("bip122:nope") == []
    finally:
        del os.environ["AWL_PAY_TO_BTC"]
        del os.environ["AWL_RPC_BTC"]
        del os.environ["AWL_RPC_BTC_SIGNET"]
    assert btcmod.btc_pay_to() == ""
    assert btcmod.rpc_urls_for(MAINNET)[0] == \
        btcmod.MAINNET_RPC_DEFAULT[0]
    assert btcmod.rpc_urls_for(NET)[0] == btcmod.SIGNET_RPC_DEFAULT[0]
