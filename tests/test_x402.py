"""awLPay real-402 tests: X-PAYMENT verification on POST /api/pay/execute.

Covers:
  (a) unpaid execute -> 402 with machine-readable x402 terms
  (b) malformed / tampered X-PAYMENT -> 402 with a reason
  (c) payment-hash replay -> 409, idempotency-key replay -> 409, and
      proof that a failed body/quote does NOT burn the payment
  (d) a fabricated-but-correctly-signed payment proof against a mock
      chain reader -> 200 + Ed25519-signed receipt, plus a read-only
      integration check against a REAL Base Sepolia USDC transfer
  (e) the local-dev bypass is OFF by default (and the retired
      AWL_TEST_MODE with it); it only works behind AWL_LOCAL_DEV=1

The mock chain reader returns canned receipts; the payer binding
signatures are REAL EIP-191 signatures from throwaway test keys
(test-only signer below — fixed nonce, never real funds).
"""

from __future__ import annotations

import base64
import importlib
import json
import os
import sys
import threading
import urllib.request
import urllib.error

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

from server import app as appmod  # noqa: E402
from server import fees as feesmod  # noqa: E402
from server import x402 as x402mod  # noqa: E402
from server import chamber as chambermod  # noqa: E402
from server import ethsig as ethsigmod  # noqa: E402
from nacl.signing import SigningKey  # noqa: E402

PORT = 8897  # 8898 is held by a sibling workstream's server; do not touch it
RES = "https://127.0.0.1:%d/api/pay/execute" % PORT
USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
# Throwaway pay-to address (test only — never a real wallet).
PAY_TO = "0xBAd41cF0f0d5442f9A53630F8081BFd257DA019b"
BUNDLER = "0x1111111111111111111111111111111111111111"  # receipt.from != payer
TRANSFER_TOPIC = x402mod.TRANSFER_TOPIC
PRICE_UNITS = (feesmod.ROUTE_PRICE_CENTS * x402mod.UNITS_PER_CENT)
# 1c cost-plus route toll in USDC base units (wired to fees.py —
# a paid 1c call must NOT 402)

# Real historical Base Sepolia USDC transfer (read-only integration test):
# 0.001 USDC self-transfer, status ok, mined on Base Sepolia.
LIVE_TX = "0x37f52d3f7a2eb3fe5d1c4e7d84258f95286623df2e2cf11ca820e67410961e00"
LIVE_TO = "0x2e0c37b721124e2558baf75f6f8e6cc9f14aec29"


# ---------------------------------------------------------------- test-only signer
def sign_eoa(msg: bytes, priv: int):
    """Deterministic ECDSA signer over the vendored ethsig primitives.

    TEST ONLY: throwaway key, fixed nonce. Produces a REAL EIP-191
    personal_sign signature that ethsig.verify_personal_sign accepts —
    the point is that the *verification* path under test sees genuine
    signatures, while the chain data stays fabricated.
    """
    digest = ethsigmod.eth_personal_message(msg)
    z = int.from_bytes(digest, "big")
    k = 0x2A2A2A2A  # fixed test nonce — never use a real key with this
    rx, ry = ethsigmod._point_mul(k)
    r = rx % ethsigmod._N
    s = (pow(k, ethsigmod._N - 2, ethsigmod._N) * (z + r * priv)) % ethsigmod._N
    parity = ry & 1
    # Wallet-shaped low-s normalization: real personal_sign wallets emit
    # low-s signatures. Negating s alone would break ecrecover (key
    # recovery is not malleability-invariant), so the v parity flips with
    # it — then r^-1(s'R - zG) lands back on the true key, and the
    # verifier's low-s malleability guard accepts the signature.
    if s > ethsigmod._N // 2:
        s, parity = ethsigmod._N - s, parity ^ 1
    v = 27 + parity  # r = R.x < N here, so recid is just R.y parity
    qx, qy = ethsigmod._point_mul(priv)
    addr = ethsigmod.pubkey_to_address((qx, qy))
    sig = r.to_bytes(32, "big") + s.to_bytes(32, "big") + bytes([v])
    return "0x" + addr.hex(), "0x" + sig.hex()


def _topic_addr(a: str) -> str:
    return "0x" + "00" * 12 + a[2:].lower()


def _transfer_log(usdc: str, frm: str, to: str, value: int) -> dict:
    return {"address": usdc,
            "topics": [TRANSFER_TOPIC, _topic_addr(frm), _topic_addr(to)],
            "data": hex(value)}


MOCK_RECEIPTS: dict[str, dict] = {}


def make_mock_rpc(receipts):
    def call(method, params):
        if method == "eth_getTransactionReceipt":
            return {"result": receipts.get((params[0] or "").lower())}
        if method == "eth_getCode":
            return {"result": "0x"}  # every mock payer is an EOA
        raise AssertionError("unexpected rpc method: " + method)
    return call


_TX_COUNTER = [0]


def new_payment(amount_units: int = PRICE_UNITS, payer_priv: int = 0x12345,
                network: str = "eip155:8453"):
    """Register a fabricated payment in the mock chain and return
    (tx_hash, X-PAYMENT header value, payer address).

    The receipt's `from` is a bundler address on purpose: the verifier
    must bind to the TOKEN SENDER from the Transfer log, not tx.from.
    """
    _TX_COUNTER[0] += 1
    txh = "0x%064x" % (0xC0FFEE + _TX_COUNTER[0])
    payer_addr, payer_sig = sign_eoa(
        x402mod.binding_message(txh, RES), payer_priv)
    MOCK_RECEIPTS[txh.lower()] = {
        "status": "0x1",
        "from": BUNDLER,
        "logs": [_transfer_log(USDC_BASE, payer_addr, PAY_TO, amount_units)],
    }
    header = base64.urlsafe_b64encode(json.dumps({
        "x402Version": 2,
        "scheme": "exact",
        "network": network,
        "payload": {"txHash": txh, "payerSig": payer_sig},
    }).encode()).decode("ascii")
    return txh, header, payer_addr


def _post(port, path, body, headers=None):
    req = urllib.request.Request(
        "http://127.0.0.1:%d%s" % (port, path),
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST")
    try:
        # 60s: this VM is shared with sibling workstreams, and the
        # vendored pure-Python secp256k1 verification is CPU-bound.
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.load(r), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode()), dict(e.headers)


def _execute_body(idempotency_key=None):
    body = {"from_chain": "ethereum", "from_token": "ETH",
            "to_chain": "base", "to_token": "USDC",
            "amount_cents": 10_000, "tier": 1,
            "volume_used_cents": 0, "txs_used": 0}
    if idempotency_key is not None:
        body["idempotency_key"] = idempotency_key
    return body


# ---------------------------------------------------------------- fixture
@pytest.fixture(scope="module")
def x402_server():
    # Env isolation: the local-dev bypass and the retired test-mode flag
    # must be OFF unless a single test opts in (and restores after).
    saved = {}
    for k in ("AWL_LOCAL_DEV", "AWL_TEST_MODE", "AWL_PAY_TO",
              "AWL_RPC_BASE", "AWL_RELAYER_KEY", "AWL_RPC_BASE_SEPOLIA",
              "AWL_X402_STATE"):
        saved[k] = os.environ.get(k)
        os.environ.pop(k, None)
    os.environ["AWL_PAY_TO"] = PAY_TO
    # Dummy URL: the rail counts as configured (so the 402 lists Base),
    # but the mock chain reader intercepts every verification call —
    # no live chain is ever touched by the HTTP tests.
    os.environ["AWL_RPC_BASE"] = "http://127.0.0.1:9/"
    # Fixed relayer seed (test-only) so the receipt signature is verifiable.
    os.environ["AWL_RELAYER_KEY"] = "a5" * 32
    x402mod.reset_used_for_tests()
    x402mod.set_test_rpc(make_mock_rpc(MOCK_RECEIPTS))
    # Reload so the module picks up this fixture's env (PORT etc.) — the
    # other test module may have imported it first with different env.
    importlib.reload(appmod)
    import uvicorn
    config = uvicorn.Config(appmod.app, host="127.0.0.1", port=PORT,
                            log_level="critical", access_log=False,
                            lifespan="off")
    srv = uvicorn.Server(config)
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    import time
    for _ in range(50):
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/health" % PORT,
                                        timeout=2) as r:
                assert r.status == 200
                break
        except Exception:
            time.sleep(0.1)
    yield PORT
    srv.should_exit = True
    t.join(timeout=15)
    x402mod.clear_test_rpc()
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


# ---------------------------------------------------------------- (a) unpaid -> 402 with machine-readable terms
def test_unpaid_execute_402_with_terms(x402_server):
    code, body, headers = _post(x402_server, "/api/pay/execute",
                                _execute_body())
    assert code == 402
    assert body["x402Version"] == 2
    assert "payment required" in body["error"]
    assert body["accepts"], "402 must list at least one verifiable rail"
    first = body["accepts"][0]
    assert first["scheme"] == "exact"
    assert first["network"] == "eip155:8453"
    assert first["amount"] == str(PRICE_UNITS)  # 1c route toll in base units
    assert first["asset"] == USDC_BASE
    assert first["payTo"] == PAY_TO
    assert first["resource"].endswith("/api/pay/execute")
    assert "awlpay payment proof" in first["extra"]["howto"]
    assert "payerSig" in first["extra"]["howto"]
    assert {k.lower(): v for k, v in headers.items()}.get(
        "payment-required") == "1"


# ---------------------------------------------------------------- (b) malformed / tampered -> 402 with reason
def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii")


def _xpay(obj) -> str:
    return _b64url(json.dumps(obj).encode())


@pytest.mark.parametrize("header,fragment", [
    ("not-valid-base64!!!", "base64url"),
    (_b64url(b"hello"), "JSON"),
    (_b64url(b"[1,2,3]"), "JSON object"),
    (_xpay({"scheme": "eip3009"}), "unsupported scheme"),
    (_xpay({"scheme": "exact", "network": "eip155:999999",
            "payload": {"txHash": "0x" + "aa" * 32,
                        "payerSig": "0x" + "bb" * 65}}),
     "unsupported network"),
    (_xpay({"scheme": "exact", "payload": {}}), "payload.txHash"),
    (_xpay({"scheme": "exact",
            "payload": {"txHash": "0x" + "aa" * 32}}), "payerSig"),
    (_xpay({"scheme": "exact",
            "payload": {"txHash": "0x1234",
                        "payerSig": "0x" + "bb" * 65}}), "bad txhash format"),
])
def test_malformed_x_payment_402s(x402_server, header, fragment):
    code, body, _ = _post(x402_server, "/api/pay/execute", _execute_body(),
                          {"X-PAYMENT": header})
    assert code == 402, (header, body)
    assert body["x402Version"] == 2
    assert fragment in body.get("reason", ""), body
    assert body["accepts"], "malformed payment must still get the terms"


def test_unknown_tx_hash_402s(x402_server):
    txh = "0x" + "de" * 32  # well-formed, but the mock chain has no receipt
    _, sig = sign_eoa(x402mod.binding_message(txh, RES), 0x12345)
    code, body, _ = _post(
        x402_server, "/api/pay/execute", _execute_body(),
        {"X-PAYMENT": _xpay({"scheme": "exact",
                             "payload": {"txHash": txh,
                                         "payerSig": sig}})})
    assert code == 402
    assert "tx not found" in body["reason"], body


def test_tampered_payer_sig_wrong_signer_402s(x402_server):
    # Receipt pays from P1, but the binding is signed by P2's key.
    txh, _, payer1 = new_payment()
    _, sig2 = sign_eoa(x402mod.binding_message(txh, RES), 0x99999)
    code, body, _ = _post(
        x402_server, "/api/pay/execute", _execute_body(),
        {"X-PAYMENT": _xpay({"scheme": "exact",
                             "payload": {"txHash": txh,
                                         "payerSig": sig2}})})
    assert code == 402
    assert "does not match" in body["reason"], body
    assert payer1.lower() in body["reason"]


def test_tampered_payer_sig_bitflip_402s(x402_server):
    txh, header, _ = new_payment()
    payload = json.loads(base64.urlsafe_b64decode(header + "==").decode())
    sig = payload["payload"]["payerSig"]
    flipped = sig[:-1] + ("0" if sig[-1] != "0" else "1")
    payload["payload"]["payerSig"] = flipped
    code, body, _ = _post(
        x402_server, "/api/pay/execute", _execute_body(),
        {"X-PAYMENT": _xpay(payload)})
    assert code == 402, body
    assert "reason" in body


def test_underpaid_402s(x402_server):
    txh, header, _ = new_payment(amount_units=1000)  # 0.1c < 1c price
    code, body, _ = _post(x402_server, "/api/pay/execute", _execute_body(),
                          {"X-PAYMENT": header})
    assert code == 402
    assert "underpaid" in body["reason"], body


def test_txhash_scheme_compat_accepted(x402_server):
    # Older rider-x402-style integrators send scheme "txHash".
    txh, _, payer = new_payment()
    _, sig = sign_eoa(x402mod.binding_message(txh, RES), 0x12345)
    code, body, _ = _post(
        x402_server, "/api/pay/execute", _execute_body(),
        {"X-PAYMENT": _xpay({"scheme": "txHash",
                             "payload": {"txHash": txh,
                                         "payerSig": sig}})})
    assert code == 200 and body["ok"] is True, body
    assert body["payment"]["payer"] == payer.lower()


# ---------------------------------------------------------------- (c) replays -> 409
def test_payment_hash_replay_409(x402_server):
    txh, header, _ = new_payment()
    code, first, _ = _post(x402_server, "/api/pay/execute",
                           _execute_body(idempotency_key="replay-a-1"),
                           {"X-PAYMENT": header})
    assert code == 200 and first["ok"] is True, first
    # Same payment proof, fresh idempotency key -> 409 payment replay.
    code, second, _ = _post(x402_server, "/api/pay/execute",
                            _execute_body(idempotency_key="replay-a-2"),
                            {"X-PAYMENT": header})
    assert code == 409, second
    assert "payment already used" in second["error"]


def test_idempotency_key_replay_409(x402_server):
    _, header_a, _ = new_payment()
    code, first, _ = _post(x402_server, "/api/pay/execute",
                           _execute_body(idempotency_key="replay-b-1"),
                           {"X-PAYMENT": header_a})
    assert code == 200 and first["ok"] is True, first
    # Fresh payment, reused idempotency key -> 409 nonce replay, and the
    # fresh payment must NOT be burned by it.
    txh_b, header_b, _ = new_payment()
    code, second, _ = _post(x402_server, "/api/pay/execute",
                            _execute_body(idempotency_key="replay-b-1"),
                            {"X-PAYMENT": header_b})
    assert code == 409, second
    assert "idempotency_key" in second["error"]
    # The unburned payment still spends afterwards.
    code, third, _ = _post(x402_server, "/api/pay/execute",
                           _execute_body(idempotency_key="replay-b-2"),
                           {"X-PAYMENT": header_b})
    assert code == 200 and third["ok"] is True, third


def test_bad_body_does_not_burn_payment(x402_server):
    # A payment that fails the body/quote stage stays spendable.
    _, header, _ = new_payment()
    bad = _execute_body()
    bad["amount_cents"] = "lots"  # malformed -> 200 refusal, not execution
    code, refused, _ = _post(x402_server, "/api/pay/execute", bad,
                             {"X-PAYMENT": header})
    assert code == 200 and refused.get("refused") is True, refused
    code, ok_body, _ = _post(x402_server, "/api/pay/execute",
                             _execute_body(idempotency_key="replay-c-1"),
                             {"X-PAYMENT": header})
    assert code == 200 and ok_body["ok"] is True, ok_body


# ---------------------------------------------------------------- (d) valid proof -> 200 + signed receipt
def test_valid_payment_200_signed_receipt(x402_server):
    txh, header, payer = new_payment()
    code, body, headers = _post(x402_server, "/api/pay/execute",
                                _execute_body(idempotency_key="valid-1"),
                                {"X-PAYMENT": header})
    assert code == 200 and body["ok"] is True, body
    assert body["charged_cents"] == feesmod.ROUTE_PRICE_CENTS == 1
    assert body["payment"]["via"] == "x402"
    assert body["payment"]["network"] == "eip155:8453"
    assert body["payment"]["tx"] == txh
    assert body["payment"]["payer"] == payer.lower()
    assert body["payment"]["paid_units"] == PRICE_UNITS
    # The receipt signature verifies against the fixture's relayer key.
    sk = SigningKey(bytes.fromhex("a5" * 32))
    env = body["attestation"]
    assert chambermod.verify_attestation(env, sk.verify_key)
    receipt = json.loads(env["payload"])
    assert receipt["mode"] == "mock" and receipt["net_cents"] == 10_000
    # A corrupted payload must NOT verify against the same signature.
    tampered = env["payload"].replace('"net_cents":10000', '"net_cents":10001')
    assert tampered != env["payload"]
    assert not chambermod.verify_attestation(
        dict(env, payload=tampered), sk.verify_key)
    # X-PAYMENT-RESPONSE acknowledges the settled payment.
    resp_h = {k.lower(): v for k, v in headers.items()}.get("x-payment-response")
    assert resp_h, "missing X-PAYMENT-RESPONSE header"
    ack = json.loads(base64.urlsafe_b64decode(resp_h + "==").decode())
    assert ack["success"] is True and ack["transaction"] == txh


def test_sol_usdc_delta_sums():
    # Pure-function check of the ported Solana balance-delta summation.
    mint = x402mod.sol_usdc_mint()
    owner = "SomeOwner111111111111111111111111111111111"
    meta = {
        "preTokenBalances": [{"accountIndex": 1, "mint": mint, "owner": owner,
                               "uiTokenAmount": {"amount": "100"}}],
        "postTokenBalances": [{"accountIndex": 1, "mint": mint, "owner": owner,
                                "uiTokenAmount": {"amount": "300"}}],
    }
    assert x402mod._sum_sol_usdc_to(meta, owner) == 200
    assert x402mod._sum_sol_usdc_to(meta, "NobodyElse111111111111111111111") == 0


def test_solana_parse_needs_pay_to():
    # Solana rail is parsed but refused when AWL_PAY_TO_SOL is unset.
    code_payload = _xpay({"scheme": "exact",
                          "network": x402mod.SOLANA_NETWORK,
                          "payload": {"signature": "5" * 88}})
    proof, net, psig, err = x402mod.parse_x_payment(code_payload)
    assert err == "Solana rail not enabled on this server"


def test_real_base_sepolia_transfer_reads():
    """Read-only integration check against a REAL Base Sepolia USDC
    transfer: the verifier must walk the live chain (format, receipt,
    status, Transfer-log sum) and stop exactly at the payer-binding
    step, which no stranger's key can satisfy.

    No funds move, no keys are used — strictly reads via public RPC.
    Skips (with the reason) if the testnet is unreachable.
    """
    saved = os.environ.get("AWL_RPC_BASE_SEPOLIA")
    os.environ["AWL_RPC_BASE_SEPOLIA"] = "https://sepolia.base.org"
    info: dict = {"reason": "not attempted"}
    ok = False
    last_err: Exception | None = None
    try:
        # Public RPCs are flaky (dropped reads, closed connections); retry
        # a few times — on exceptions AND on "rpc unreachable" results —
        # before calling the testnet unreachable.
        for _ in range(4):
            try:
                ok, info = x402mod.verify_payment(
                    LIVE_TX, x402mod.BASE_SEPOLIA, 1, LIVE_TO,
                    set(), rpc=None, payer_sig=None,
                    resource="https://127.0.0.1:%d/api/pay/execute" % PORT)
                last_err = None
            except Exception as e:  # noqa: BLE001 - network is best-effort
                last_err = e
                continue
            if not str(info.get("reason", "")).startswith("rpc unreachable"):
                break
        else:
            info = {"reason": "rpc unreachable (persistent)"}
        if last_err is not None:
            pytest.skip("Base Sepolia unreachable: %s" % last_err)
        if str(info.get("reason", "")).startswith("rpc unreachable"):
            pytest.skip("Base Sepolia flaky: %s" % info["reason"])
    finally:
        if saved is None:
            os.environ.pop("AWL_RPC_BASE_SEPOLIA", None)
        else:
            os.environ["AWL_RPC_BASE_SEPOLIA"] = saved
    # The receipt exists (else "tx not found"), status is ok (else
    # "reverted"), and the Transfer logs summed 1000 units >= 1 (else
    # "no USDC transfer") — so the ONLY legal failure is the missing
    # payer binding.
    assert ok is False
    assert info["reason"].startswith("missing payerSig"), info


# ---------------------------------------------------------------- (e) local-dev bypass: off by default
def test_local_dev_bypass_off_by_default(x402_server):
    assert os.environ.get("AWL_LOCAL_DEV") != "1"
    code, body, _ = _post(x402_server, "/api/pay/execute", _execute_body(),
                          {"X-Test-Payment": "ok"})
    assert code == 402, body  # the header alone buys nothing


def test_legacy_test_mode_is_dead(x402_server):
    os.environ["AWL_TEST_MODE"] = "1"
    try:
        assert os.environ.get("AWL_LOCAL_DEV") != "1"
        code, body, _ = _post(x402_server, "/api/pay/execute",
                              _execute_body(),
                              {"X-Test-Payment": "ok"})
        assert code == 402, body  # AWL_TEST_MODE no longer enables anything
    finally:
        os.environ.pop("AWL_TEST_MODE", None)


def test_local_dev_bypass_on_with_flag(x402_server):
    os.environ["AWL_LOCAL_DEV"] = "1"
    try:
        code, body, _ = _post(x402_server, "/api/pay/execute",
                              _execute_body(idempotency_key="localdev-1"),
                              {"X-Test-Payment": "ok"})
        assert code == 200 and body["ok"] is True, body
        assert body["payment"] == {"via": "local-dev", "verified": False}
    finally:
        os.environ.pop("AWL_LOCAL_DEV", None)
