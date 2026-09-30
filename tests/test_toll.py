"""AwLPay toll deposit-verify tests: POST /internal/toll/deposit/verify.

Covers (all with a mock chain reader + REAL EIP-191 signatures from
throwaway test keys — the verification path sees genuine signatures
while the chain data stays fabricated):
  (a) unauthenticated / wrong secret -> 403
  (b) TOLL_MAINNET_AUTHORIZED != 1 -> 403 (mainnet gate holds)
  (c) valid escrow deposit -> verified:true with paid amount + payer
  (d) deposit to the wrong wallet -> verified:false
  (e) underpaid deposit -> verified:false
  (f) signature from the wrong key -> verified:false (front-runner blocked)
  (g) AWL_USDC_BASE override set -> 403 (canonical-USDC-only law)
  (h) TOLL_RPC_BASE unset and no test seam -> 403 (no public fallback)
  (i) reverted tx (status 0x0) -> verified:false
"""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

from starlette.testclient import TestClient  # noqa: E402

from server import app as appmod  # noqa: E402
from server import x402 as x402mod  # noqa: E402
from server import ethsig as ethsigmod  # noqa: E402
from server import toll as tollmod  # noqa: E402

USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
# Throwaway slot wallets (test only — never real wallets).
ESCROW_WALLET = "0xe5c7e5c7e5c7e5c7e5c7e5c7e5c7e5c7e5c7e5c7"
BONDS_WALLET = "0x0b5c0b5c0b5c0b5c0b5c0b5c0b5c0b5c0b5c0b5c"
TRANSFER_TOPIC = x402mod.TRANSFER_TOPIC
SECRET = "test-toll-secret"


# ---------------------------------------------------------------- test-only signer
def sign_eoa(msg: bytes, priv: int):
    """Deterministic ECDSA signer (same construction as test_x402.py).

    TEST ONLY: throwaway key, fixed nonce. Real EIP-191 personal_sign.
    """
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


_TX = [0]


def new_deposit(slot="escrow", amount_units=1_000_000, payer_priv=0xBEEF,
                to_wallet=None, status="0x1"):
    """Register a fabricated deposit; return (tx_hash, payer_sig, payer)."""
    _TX[0] += 1
    txh = "0x%064x" % (0xD0E9 + _TX[0])
    to_wallet = to_wallet or {"escrow": ESCROW_WALLET,
                              "bonds": BONDS_WALLET}[slot]
    ref = "job:test-%d" % _TX[0]
    msg = x402mod.binding_message(txh, "toll:%s:%s" % (slot, ref))
    payer_addr, payer_sig = sign_eoa(msg, payer_priv)
    MOCK_RECEIPTS[txh.lower()] = {
        "status": status,
        "from": "0x1111111111111111111111111111111111111111",
        "logs": [_transfer_log(USDC_BASE, payer_addr, to_wallet,
                               amount_units)],
    }
    return txh, payer_sig, payer_addr, ref


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("TOLL_SERVICE_SECRET", SECRET)
    monkeypatch.setenv("TOLL_MAINNET_AUTHORIZED", "1")
    monkeypatch.setenv("TOLL_ESCROW_WALLET", ESCROW_WALLET)
    monkeypatch.setenv("TOLL_BONDS_WALLET", BONDS_WALLET)
    monkeypatch.delenv("AWL_USDC_BASE", raising=False)
    monkeypatch.delenv("TOLL_RPC_BASE", raising=False)
    MOCK_RECEIPTS.clear()
    x402mod.set_test_rpc(make_mock_rpc(MOCK_RECEIPTS))
    yield TestClient(appmod.app)
    x402mod.clear_test_rpc()


def _post(client, body, secret=SECRET):
    return client.post("/internal/toll/deposit/verify", json=body,
                       headers={"X-Toll-Service-Secret": secret})


def test_auth_required(client):
    txh, sig, _, ref = new_deposit()
    r = client.post("/internal/toll/deposit/verify",
                    json={"slot": "escrow", "tx_hash": txh,
                          "payer_sig": sig, "min_uusdc": 1_000_000,
                          "ref": ref})
    assert r.status_code == 403
    r = _post(client, {"slot": "escrow", "tx_hash": txh,
                       "payer_sig": sig, "min_uusdc": 1_000_000,
                       "ref": ref}, secret="wrong")
    assert r.status_code == 403


def test_mainnet_gate(client, monkeypatch):
    monkeypatch.setenv("TOLL_MAINNET_AUTHORIZED", "0")
    txh, sig, _, ref = new_deposit()
    r = _post(client, {"slot": "escrow", "tx_hash": txh, "payer_sig": sig,
                       "min_uusdc": 1_000_000, "ref": ref})
    assert r.status_code == 403
    assert r.json()["verified"] is False


def test_valid_escrow_deposit(client):
    txh, sig, payer, ref = new_deposit(amount_units=5_000_000)
    r = _post(client, {"slot": "escrow", "tx_hash": txh, "payer_sig": sig,
                       "min_uusdc": 5_000_000, "ref": ref})
    assert r.status_code == 200
    body = r.json()
    assert body["verified"] is True
    assert body["paid_uusdc"] == 5_000_000
    assert body["payer"] == payer.lower()
    assert body["chain_id"] == 8453
    assert body["token"] == USDC_BASE


def test_valid_bonds_deposit(client):
    txh, sig, payer, ref = new_deposit(slot="bonds", amount_units=2_000_000)
    r = _post(client, {"slot": "bonds", "tx_hash": txh, "payer_sig": sig,
                       "min_uusdc": 2_000_000, "ref": ref})
    assert r.status_code == 200
    assert r.json()["verified"] is True


def test_wrong_wallet_refused(client):
    txh, sig, _, ref = new_deposit(
        to_wallet="0x000000000000000000000000000000000000dEaD")
    r = _post(client, {"slot": "escrow", "tx_hash": txh, "payer_sig": sig,
                       "min_uusdc": 1_000_000, "ref": ref})
    assert r.json()["verified"] is False
    assert "no USDC transfer" in r.json()["reason"]


def test_underpaid_refused(client):
    txh, sig, _, ref = new_deposit(amount_units=999_999)
    r = _post(client, {"slot": "escrow", "tx_hash": txh, "payer_sig": sig,
                       "min_uusdc": 1_000_000, "ref": ref})
    assert r.json()["verified"] is False
    assert "underpaid" in r.json()["reason"]


def test_wrong_signer_refused(client):
    # Front-runner: copies the tx hash but signs with their own key.
    txh, _, _, ref = new_deposit(payer_priv=0xBEEF)
    msg = x402mod.binding_message(txh, "toll:escrow:%s" % ref)
    _, evil_sig = sign_eoa(msg, 0xBAD)
    r = _post(client, {"slot": "escrow", "tx_hash": txh,
                       "payer_sig": evil_sig, "min_uusdc": 1_000_000,
                       "ref": ref})
    assert r.json()["verified"] is False


def test_canonical_usdc_only(client, monkeypatch):
    monkeypatch.setenv("AWL_USDC_BASE",
                       "0x0000000000000000000000000000000000000001")
    txh, sig, _, ref = new_deposit()
    r = _post(client, {"slot": "escrow", "tx_hash": txh, "payer_sig": sig,
                       "min_uusdc": 1_000_000, "ref": ref})
    assert r.status_code == 403
    assert "canonical" in r.json()["reason"]


def test_no_rpc_no_fallback(client):
    x402mod.clear_test_rpc()  # no seam, no TOLL_RPC_BASE
    txh, sig, _, ref = new_deposit()
    r = _post(client, {"slot": "escrow", "tx_hash": txh, "payer_sig": sig,
                       "min_uusdc": 1_000_000, "ref": ref})
    assert r.status_code == 403
    assert "TOLL_RPC_BASE" in r.json()["reason"]


def test_reverted_tx_refused(client):
    txh, sig, _, ref = new_deposit(status="0x0")
    r = _post(client, {"slot": "escrow", "tx_hash": txh, "payer_sig": sig,
                       "min_uusdc": 1_000_000, "ref": ref})
    assert r.json()["verified"] is False
    assert "reverted" in r.json()["reason"]


def test_method_parity(client):
    r = client.get("/internal/toll/deposit/verify",
                   headers={"X-Toll-Service-Secret": SECRET})
    assert r.status_code == 404


# --------------------------------------------------------------------------
# POST /internal/toll/send — money-moving endpoint, stubbed chain client.
# --------------------------------------------------------------------------

class _FakeEth:
    def __init__(self):
        self.chain_id = 8453
        self.sent = []

    def get_transaction_count(self, addr):
        return 3

    @property
    def gas_price(self):
        return 1_000_000_000

    def send_raw_transaction(self, raw: bytes):
        h = "0x%064x" % (0x5E4D + len(self.sent))
        self.sent.append(bytes(raw))
        return bytes.fromhex(h[2:])


class _FakeW3:
    def __init__(self):
        self.eth = _FakeEth()


@pytest.fixture()
def send_client(monkeypatch, tmp_path):
    monkeypatch.setenv("TOLL_SERVICE_SECRET", SECRET)
    monkeypatch.setenv("TOLL_MAINNET_AUTHORIZED", "1")
    monkeypatch.setenv("TOLL_RPC_BASE", "http://127.0.0.1:9/")
    monkeypatch.setenv("TOLL_ESCROW_KEY", "ab" * 32)
    monkeypatch.setenv("TOLL_BONDS_KEY", "cd" * 32)
    monkeypatch.setenv("TOLL_STATE", str(tmp_path / "state.json"))
    monkeypatch.setenv("TOLL_LEDGER", str(tmp_path / "ledger.jsonl"))
    monkeypatch.delenv("AWL_USDC_BASE", raising=False)
    from server import chains as chainsmod  # noqa: E402
    fake = _FakeW3()
    monkeypatch.setattr(chainsmod.EvmAdapter, "_w3client",
                        lambda self: fake)
    return TestClient(appmod.app), fake


def _send(client, body, secret=SECRET):
    return client.post("/internal/toll/send", json=body,
                       headers={"X-Toll-Service-Secret": secret})


def test_send_auth_required(send_client):
    client, _ = send_client
    r = client.post("/internal/toll/send", json={})
    assert r.status_code == 403


def test_send_happy_path(send_client):
    client, fake = send_client
    to = "0x" + "ef" * 20
    r = _send(client, {"slot": "escrow", "to_address": to,
                       "amount_uusdc": 990_000,
                       "idempotency_key": "rel-1", "purpose": "escrow_release"})
    assert r.status_code == 200
    body = r.json()
    assert body["sent"] is True
    assert body["tx_hash"].startswith("0x")
    assert body["idempotent"] is False
    assert len(fake.eth.sent) == 1


def test_send_idempotent_retry(send_client):
    client, fake = send_client
    to = "0x" + "ef" * 20
    body = {"slot": "escrow", "to_address": to, "amount_uusdc": 990_000,
            "idempotency_key": "rel-dup", "purpose": "escrow_release"}
    first = _send(client, body).json()
    second = _send(client, body).json()
    assert second["sent"] is True and second["idempotent"] is True
    assert second["tx_hash"] == first["tx_hash"]
    assert len(fake.eth.sent) == 1


def test_send_cap_refused(send_client, monkeypatch):
    from server import chains as chainsmod  # noqa: E402
    monkeypatch.setattr(chainsmod, "TOLL_MAX_TX_UUSDC", 100)
    client, fake = send_client
    r = _send(client, {"slot": "escrow", "to_address": "0x" + "ef" * 20,
                       "amount_uusdc": 101, "idempotency_key": "cap-1",
                       "purpose": "escrow_release"})
    assert r.json()["sent"] is False
    assert len(fake.eth.sent) == 0


def test_send_needs_mainnet_auth(send_client, monkeypatch):
    monkeypatch.setenv("TOLL_MAINNET_AUTHORIZED", "0")
    client, fake = send_client
    r = _send(client, {"slot": "escrow", "to_address": "0x" + "ef" * 20,
                       "amount_uusdc": 100, "idempotency_key": "auth-1",
                       "purpose": "escrow_release"})
    assert r.status_code == 403
    assert r.json()["sent"] is False
    assert len(fake.eth.sent) == 0


def test_send_method_parity(send_client):
    client, _ = send_client
    r = client.get("/internal/toll/send",
                   headers={"X-Toll-Service-Secret": SECRET})
    assert r.status_code == 404


# --------------------------------------------------------------------------
# network="sepolia" selection on the toll endpoints.
# --------------------------------------------------------------------------

def test_verify_network_rejected(client):
    r = client.post("/internal/toll/deposit/verify",
                    json={"slot": "escrow", "tx_hash": "0x" + "aa" * 32,
                          "payer_sig": "0x00", "min_uusdc": 100,
                          "ref": "job-1", "network": "bogus"},
                    headers={"X-Toll-Service-Secret": SECRET})
    assert r.status_code == 200
    assert r.json()["verified"] is False


def test_verify_sepolia_needs_flag(client, monkeypatch):
    monkeypatch.delenv("TOLL_TESTNET_SEPOLIA", raising=False)
    r = client.post("/internal/toll/deposit/verify",
                    json={"slot": "escrow", "tx_hash": "0x" + "aa" * 32,
                          "payer_sig": "0x00", "min_uusdc": 100,
                          "ref": "job-1", "network": "sepolia"},
                    headers={"X-Toll-Service-Secret": SECRET})
    assert r.status_code == 403
    assert r.json()["verified"] is False


def test_send_network_rejected(send_client):
    client, fake = send_client
    r = _send(client, {"slot": "escrow", "to_address": "0x" + "ef" * 20,
                       "amount_uusdc": 100, "idempotency_key": "net-1",
                       "purpose": "x", "network": "bogus"})
    assert r.json()["sent"] is False
    assert len(fake.eth.sent) == 0


def test_send_sepolia_needs_flag(send_client, monkeypatch):
    monkeypatch.delenv("TOLL_TESTNET_SEPOLIA", raising=False)
    client, fake = send_client
    r = _send(client, {"slot": "escrow", "to_address": "0x" + "ef" * 20,
                       "amount_uusdc": 100, "idempotency_key": "net-2",
                       "purpose": "x", "network": "sepolia"})
    assert r.status_code == 403
    assert r.json()["sent"] is False
    assert len(fake.eth.sent) == 0


def test_send_sepolia_ok_when_enabled(send_client, monkeypatch):
    monkeypatch.setenv("TOLL_TESTNET_SEPOLIA", "1")
    monkeypatch.setenv("TOLL_RPC_SEPOLIA", "http://127.0.0.1:9/")
    monkeypatch.setenv("TOLL_TESTNET_ESCROW_KEY", "ab" * 32)
    from server import chains as chainsmod  # noqa: E402

    class _Eth:
        chain_id = 84532
        sent = []

        def get_transaction_count(self, addr):
            return 1

        @property
        def gas_price(self):
            return 1_000_000_000

        def send_raw_transaction(self, raw: bytes):
            self.sent.append(bytes(raw))
            return bytes.fromhex("22" * 32)

    class _W3:
        eth = _Eth()

    monkeypatch.setattr(chainsmod.EvmAdapter, "_w3client",
                        lambda self: _W3())
    client, _ = send_client
    r = _send(client, {"slot": "escrow", "to_address": "0x" + "ef" * 20,
                       "amount_uusdc": 100, "idempotency_key": "net-3",
                       "purpose": "sepolia_e2e", "network": "sepolia"})
    body = r.json()
    assert r.status_code == 200, body
    assert body["sent"] is True
    # The tx hash is the deterministic hash of the signed bytes (known
    # before broadcast), not the node's echo — 0x + 64 hex either way.
    assert body["tx_hash"].startswith("0x") and len(body["tx_hash"]) == 66
