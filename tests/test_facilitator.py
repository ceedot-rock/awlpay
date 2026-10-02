"""Tests for the x402 facilitator (server/facilitator.py).

Push-payment model: /supported advertises rails, /verify checks proofs
against chain state (mocked), /settle verifies + consumes replay keys.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from server import facilitator as fac
from server import x402


def _env():
    os.environ["AWL_PAY_TO_TRON"] = "TXYZopYRdj2D9XRtbG411XZZ3kM5VkAeBf"
    os.environ["AWL_PAY_TO_STELLAR"] = (
        "GAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAWHF")
    os.environ["AWL_PAY_TO_BTC"] = (
        "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4")


def test_supported_kinds_lists_rails():
    _env()
    kinds = fac.supported_kinds()
    assert isinstance(kinds, list)
    assert len(kinds) >= 6  # EVM + Tron + Stellar + Bitcoin at minimum
    networks = {k["network"] for k in kinds}
    assert x402.TRON_NILE in networks
    assert x402.STELLAR_TESTNET in networks
    assert x402.BTC_SIGNET in networks
    for k in kinds:
        assert k["x402Version"] == 2
        assert k["scheme"] == "exact"


def test_supported_response_shape():
    _env()
    resp = fac.supported_response()
    assert "kinds" in resp
    assert "extensions" in resp
    assert "signers" in resp
    assert isinstance(resp["kinds"], list)


def test_discovery_manifest():
    _env()
    m = fac.discovery_manifest("awlpay.fly.dev")
    assert m["name"] == "AwLPay Facilitator"
    assert m["model"] == "push-payment"
    assert m["endpoints"]["verify"] == "https://awlpay.fly.dev/verify"
    assert len(m["kinds"]) > 0


def test_verify_rejects_bad_version():
    r = fac.facilitator_verify({"x402Version": 1})
    assert r["isValid"] is False
    assert r["invalidReason"] == "invalid_x402_version"


def test_verify_rejects_non_dict():
    r = fac.facilitator_verify(None)
    assert r["isValid"] is False
    assert r["invalidReason"] == "invalid_payload"


def test_verify_rejects_missing_txhash():
    body = {
        "x402Version": 2,
        "paymentPayload": {
            "x402Version": 2,
            "accepted": {"network": x402.TRON_NILE, "amount": "10000",
                         "payTo": "TXYZopYRdj2D9XRtbG411XZZ3kM5VkAeBf"},
            "payload": {},
        },
        "paymentRequirements": {"network": x402.TRON_NILE,
                                "amount": "10000"},
    }
    r = fac.facilitator_verify(body)
    assert r["isValid"] is False
    assert r["invalidReason"] == "invalid_payload"


def test_verify_rejects_bad_amount():
    body = {
        "x402Version": 2,
        "paymentPayload": {
            "x402Version": 2,
            "accepted": {"network": x402.TRON_NILE, "amount": "abc",
                         "payTo": "TXYZopYRdj2D9XRtbG411XZZ3kM5VkAeBf"},
            "payload": {"txHash": "00" * 32},
        },
        "paymentRequirements": {"network": x402.TRON_NILE,
                                "amount": "not-a-number"},
    }
    r = fac.facilitator_verify(body)
    assert r["isValid"] is False
    assert r["invalidReason"] == "invalid_payment_requirements"


def test_extract_proof_lightning():
    net, proof, psig, res = fac._extract_proof({
        "accepted": {"network": x402.LIGHTNING_MAINNET},
        "payload": {"preimage": "11" * 32, "paymentHash": "22" * 32},
        "resource": {"url": "https://x.test/"},
    })
    assert net == x402.LIGHTNING_MAINNET
    assert proof == ("11" * 32, "22" * 32)
    assert psig is None


def test_extract_proof_evm():
    net, proof, psig, res = fac._extract_proof({
        "accepted": {"network": "eip155:84532"},
        "payload": {"txHash": "ab" * 32, "payerSig": "0x1234"},
        "resource": {"url": "https://x.test/y"},
    })
    assert net == "eip155:84532"
    assert proof == "ab" * 32
    assert psig == "0x1234"
    assert res == "https://x.test/y"


def test_settle_rejects_invalid():
    body = {
        "x402Version": 2,
        "paymentPayload": {
            "x402Version": 2,
            "accepted": {"network": x402.TRON_NILE},
            "payload": {},
        },
        "paymentRequirements": {},
    }
    r = fac.facilitator_settle(body, used_set=set())
    assert r["success"] is False
    assert r["transaction"] == ""
    assert r["network"] == x402.TRON_NILE
    assert "errorReason" in r


def test_map_reason():
    assert fac._map_reason("no verifiable price") == \
        "invalid_payment_requirements"
    assert fac._map_reason("replay detected") == "invalid_transaction_state"
    assert fac._map_reason("something else") == "unexpected_verify_error"
