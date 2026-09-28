"""awLPay v1 tests: fee exactness, oracle/router law, chamber signatures,
settlement refusal law, and HTTP end-to-end (quote free, execute 402)."""

import json
import os
import sys
import threading
import urllib.request

import pytest

# Test the server code in THIS repo checkout (works in every worktree),
# not a hardcoded path to another checkout.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from server import fees, oracle, router, chamber, settlement  # noqa: E402
from server.oracle import MockOracle  # noqa: E402
from server.router import ConverterRouter  # noqa: E402


# ---------------------------------------------------------------- fees
def test_fee_exactness_core_cases():
    # Free: 1.0% + 25c
    assert fees.calculate_fees(10_000, 0, 0, 0) == (125, "free")
    assert fees.calculate_fees(99, 0, 0, 0) == (25, "free")      # 0 + 25
    assert fees.calculate_fees(101, 0, 0, 0) == (26, "free")     # 1 + 25
    # Pro under cap: 0 fee
    assert fees.calculate_fees(10_000, 1, 0, 0) == (0, "pro_under_cap")
    # Pro at exactly the caps: volume boundary inclusive, tx boundary exclusive
    assert fees.calculate_fees(1, 1, 2_999_999, 0) == (0, "pro_under_cap")
    assert fees.calculate_fees(1, 1, 2_999_999, 499) == (0, "pro_under_cap")
    # Pro overage: volume or txs -> free pricing
    assert fees.calculate_fees(10_000, 1, 2_999_999, 0) == (125, "pro_overage")
    assert fees.calculate_fees(10_000, 1, 0, 500) == (125, "pro_overage")
    assert fees.calculate_fees(10_000, 1, 0, 501) == (125, "pro_overage")
    # L33t: 0 fee; fair-use guard trips above 100k txs
    assert fees.calculate_fees(10_000, 2, 0, 0) == (0, "l33t")
    assert fees.calculate_fees(10_000, 2, 99_999_999, 99_999_999) == (0, "l33t_fair_use_review")
    # Negative amount refused
    assert fees.calculate_fees(-1, 0, 0, 0) == (0, "refused_negative_amount")
    assert fees.calculate_fees(-1, 2, 0, 0) == (0, "refused_negative_amount")
    # Unknown tier raises
    with pytest.raises(ValueError):
        fees.calculate_fees(100, 7, 0, 0)


def test_fee_integer_arithmetic():
    # No floats anywhere: odd cent amounts floor correctly (integer division)
    for amt in (1, 3, 33, 333, 999_999):
        fee, _ = fees.calculate_fees(amt, 0, 0, 0)
        assert fee == amt // 100 + 25
        assert isinstance(fee, int)


# ---------------------------------------------------------------- oracle
def test_mock_oracle():
    o = MockOracle({("ethereum", "ETH"): 4000.0, ("base", "USDC"): 1.0})
    assert o.get_price_usd("ethereum", "eth") == 4000.0
    assert o.has_value("BASE", "usdc")
    assert o.get_price_usd("ethereum", "DOGE") is None
    assert not o.has_value("ethereum", "DOGE")


def test_coingecko_never_raises():
    o = oracle.CoinGeckoOracle(timeout=0.001)  # guaranteed timeout
    assert o.get_price_usd("ethereum", "ETH") is None
    assert o.get_price_usd("nope", "NOPE") is None


# ---------------------------------------------------------------- router
FULL = oracle.default_mock_oracle()


def test_router_trivial_path():
    r = ConverterRouter()
    p = r.find_path("ethereum", "USDC", "ethereum", "USDC", FULL)
    assert p == [{"chain": "ethereum", "token": "USDC", "hop": "origin"}]


def test_router_bridge_and_swap():
    r = ConverterRouter()
    # ETH(ethereum) -> USDC(ethereum) swap -> USDC(base) bridge
    p = r.find_path("ethereum", "ETH", "base", "USDC", FULL)
    assert p is not None
    assert p[0]["hop"] == "origin" and p[-1] == {"chain": "base", "token": "USDC", "hop": "bridge"}
    assert [s["hop"] for s in p] == ["origin", "swap", "bridge"]
    # SOL(solana) -> USDC(solana) -> USDC(ethereum) -> ETH(ethereum)
    p2 = r.find_path("solana", "SOL", "ethereum", "ETH", FULL)
    assert p2 is not None
    assert [s["token"] for s in p2] == ["SOL", "USDC", "USDC", "ETH"]


def test_router_unpriced_token_refuses():
    o = MockOracle({("ethereum", "ETH"): 4000.0})  # USDC unpriced
    r = ConverterRouter()
    assert r.find_path("ethereum", "ETH", "base", "USDC", o) is None
    assert r.find_path("ethereum", "USDC", "ethereum", "USDC", o) is None  # trivial needs price


def test_router_unknown_token():
    r = ConverterRouter()
    assert r.find_path("ethereum", "DOGE", "base", "USDC", FULL) is None
    assert r.find_path("ethereum", "ETH", "nochain", "USDC", FULL) is None


# ---------------------------------------------------------------- chamber
def test_chamber_sign_verify():
    from nacl.signing import SigningKey
    sk = SigningKey.generate()
    env = chamber.sign_attestation({"a": 1, "b": [2, 3]}, sk)
    assert env["alg"] == "ed25519"
    assert chamber.verify_attestation(env, sk.verify_key)
    # canonical form is sorted + compact
    assert env["payload"] == '{"a":1,"b":[2,3]}'


def test_chamber_tamper_fails():
    from nacl.signing import SigningKey
    sk = SigningKey.generate()
    env = chamber.sign_attestation({"x": 9}, sk)
    tampered = dict(env, payload='{"x":10}')
    assert not chamber.verify_attestation(tampered, sk.verify_key)
    assert not chamber.verify_attestation(dict(env, sig="00" * 64), sk.verify_key)
    assert not chamber.verify_attestation({"junk": 1}, sk.verify_key)
    other = SigningKey.generate()
    assert not chamber.verify_attestation(env, other.verify_key)  # wrong key


# ---------------------------------------------------------------- settlement
def test_settlement_mock_receipt():
    from nacl.signing import SigningKey
    sk = SigningKey.generate()
    out = settlement.execute_quote(
        {"from_chain": "ethereum", "from_token": "ETH",
         "to_chain": "base", "to_token": "USDC",
         "amount_cents": 10_000, "tier": 1,
         "path": [{"chain": "ethereum", "token": "ETH", "hop": "origin"}]},
        {"volume_used_cents": 0, "txs_used": 0}, signing_key=sk)
    env = out["attestation"]
    assert chamber.verify_attestation(env, sk.verify_key)
    receipt = json.loads(env["payload"])
    assert receipt["mode"] == "mock"
    assert receipt["fee_cents"] == 0 and receipt["net_cents"] == 10_000
    assert receipt["status"] == "pro_under_cap"
    assert receipt["receipt_id"]


def test_settlement_dust_refused():
    from nacl.signing import SigningKey
    sk = SigningKey.generate()
    out = settlement.execute_quote(
        {"from_chain": "ethereum", "from_token": "USDC",
         "to_chain": "ethereum", "to_token": "USDC",
         "amount_cents": 20, "tier": 0, "path": []},
        {"volume_used_cents": 0, "txs_used": 0}, signing_key=sk)
    receipt = json.loads(out["attestation"]["payload"])
    assert receipt["refused"] is True
    assert receipt["status"] == "refused_dust_eaten_by_fees"


def test_settlement_mainnet_guard():
    from nacl.signing import SigningKey
    os.environ["AWL_MAINNET_ENABLED"] = "1"
    try:
        with pytest.raises(RuntimeError, match="mainnet"):
            settlement.execute_quote(
                {"from_chain": "a", "from_token": "b", "to_chain": "c",
                 "to_token": "d", "amount_cents": 100, "tier": 0, "path": []},
                {"volume_used_cents": 0, "txs_used": 0},
                signing_key=SigningKey.generate())
    finally:
        os.environ["AWL_MAINNET_ENABLED"] = "0"


# ---------------------------------------------------------------- HTTP end-to-end
def _post(port, path, body, headers=None):
    req = urllib.request.Request(
        "http://127.0.0.1:%d%s" % (port, path),
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.load(r), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode()), dict(e.headers)


@pytest.fixture(scope="module")
def server():
    os.environ["AWL_TEST_MODE"] = "1"
    os.environ["AWL_PORT"] = "8899"
    from server import app as appmod
    t = threading.Thread(target=appmod.run, daemon=True)
    t.start()
    import time
    for _ in range(50):
        try:
            with urllib.request.urlopen("http://127.0.0.1:8899/health", timeout=2) as r:
                assert r.status == 200
                break
        except Exception:
            time.sleep(0.1)
    yield 8899


def test_http_health(server):
    with urllib.request.urlopen("http://127.0.0.1:%d/health" % server) as r:
        assert json.load(r) == {"ok": True, "mode": "mock-local"}


def test_http_quote_ok(server):
    code, body, _ = _post(server, "/api/pay/quote", {
        "from_chain": "ethereum", "from_token": "ETH",
        "to_chain": "base", "to_token": "USDC",
        "amount_cents": 10_000, "tier": 1,
        "volume_used_cents": 0, "txs_used": 0})
    assert code == 200
    assert not body.get("refused")
    assert body["fees"]["free"]["fee_cents"] == 125
    assert body["fees"]["pro"]["status"] == "pro_under_cap"
    assert body["fees"]["l33t"]["fee_cents"] == 0
    assert body["net_cents"] == 10_000
    assert body["path"]


def test_http_quote_refusals(server):
    # unknown token
    code, body, _ = _post(server, "/api/pay/quote", {
        "from_chain": "ethereum", "from_token": "DOGE",
        "to_chain": "base", "to_token": "USDC", "amount_cents": 100})
    assert code == 200 and body == {"refused": True, "reason": "unknown_token_no_price",
                                    "detail": "DOGE on ethereum has no verifiable price"}
    # dust
    code, body, _ = _post(server, "/api/pay/quote", {
        "from_chain": "ethereum", "from_token": "USDC",
        "to_chain": "ethereum", "to_token": "USDC", "amount_cents": 20, "tier": 0})
    assert code == 200 and body["refused"] is True and body["reason"] == "dust_eaten_by_fees"
    # bad request shape still 200-refusal
    code, body, _ = _post(server, "/api/pay/quote", {"amount_cents": "lots"})
    assert code == 200 and body["reason"] == "bad_request"


def test_http_execute_402_then_test_payment(server):
    body = {"from_chain": "ethereum", "from_token": "ETH",
            "to_chain": "base", "to_token": "USDC",
            "amount_cents": 10_000, "tier": 1,
            "volume_used_cents": 0, "txs_used": 0,
            "idempotency_key": "test-nonce-1"}
    # unpaid -> 402 + requirements
    code, b402, h402 = _post(server, "/api/pay/execute", body)
    assert code == 402
    assert b402["x402Version"] == 1
    assert b402["accepts"][0]["price_cents"] == 25
    assert {k.lower(): v for k, v in h402.items()}.get("payment-required") == "1"
    # paid (test mode) -> signed receipt
    code, b200, _ = _post(server, "/api/pay/execute", body,
                          {"X-Test-Payment": "ok"})
    assert code == 200 and b200["ok"] is True
    env = b200["attestation"]
    assert env["alg"] == "ed25519"
    receipt = json.loads(env["payload"])
    assert receipt["mode"] == "mock" and receipt["net_cents"] == 10_000
    # replay -> 409
    code, b409, _ = _post(server, "/api/pay/execute", body,
                          {"X-Test-Payment": "ok"})
    assert code == 409
