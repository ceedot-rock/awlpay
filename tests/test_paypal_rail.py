"""awLPay PayPal fiat-bridge rail tests (all mocked — no network).

Covers:
  (a) pure helpers: usd<->cents math (no floats), bridge quote math
  (b) sandbox law: production base URL hard-refused, unknown base refused
  (c) credentials: missing PAYPAL_CLIENT_ID/SECRET -> clean error
  (d) mocked transport: oauth token (+caching), create/get/capture order,
      create/get payout, error paths (401 auth, 422 insufficient funds,
      403 missing scope), idempotency headers
  (e) money-movement gate: capture/payout raise without AWL_PAYPAL_PAYOUTS=1
  (f) router: USD(paypal)->USDC(base) resolves via the "fiat" hop;
      USD->ETH(base) paths through fiat+swap; unpriced USD refuses;
      pre-existing exact hop sequences unchanged
  (g) receipts: Chamber-signed bridge receipts verify; tampered/wrong-key
      fail

No mainnet, no real funds, no network. Live sandbox verification is a
manual step (see scripts/demo-paypal-bridge.py).
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

from server import paypal as pp  # noqa: E402
from server import chamber  # noqa: E402
from server.oracle import MockOracle, CoinGeckoOracle, default_mock_oracle  # noqa: E402
from server.router import ConverterRouter  # noqa: E402
from nacl.signing import SigningKey  # noqa: E402

SANDBOX = "https://api-m.sandbox.paypal.com"
PROD = "https://api-m.paypal.com"


# --------------------------------------------------------------------------
# fake transport
# --------------------------------------------------------------------------
class FakeTransport:
    """Canned (status, body) per path substring. Records calls."""

    def __init__(self, routes: dict[str, tuple[int, dict]]):
        self.routes = routes
        self.calls: list[tuple] = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, dict(headers), body))
        for needle, resp in self.routes.items():
            if needle in url:
                return resp
        return 404, {"message": "no fake route for %s" % url}

    def calls_to(self, needle: str):
        return [c for c in self.calls if needle in c[1]]


def _token_route(token="tok-abc", expires_in=3600):
    return {"/v1/oauth2/token": (200, {"access_token": token,
                                       "token_type": "Bearer",
                                       "expires_in": expires_in})}


def _adapter(routes, **env):
    t = FakeTransport(routes)
    return pp.PayPalAdapter(transport=t), t


@pytest.fixture(autouse=True)
def _creds(monkeypatch):
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("PAYPAL_CLIENT_SECRET", "test-client-secret")
    monkeypatch.delenv("AWL_PAYPAL_PAYOUTS", raising=False)


# ---------------------------------------------------------------- (a) math
def test_usd_to_cents_no_floats():
    assert pp.usd_to_cents("10.99") == 1099
    assert pp.usd_to_cents("10") == 1000
    assert pp.usd_to_cents("0.01") == 1
    assert pp.usd_to_cents("0.1") == 10
    assert pp.usd_to_cents("  5.00  ") == 500
    with pytest.raises(ValueError):
        pp.usd_to_cents("10.999")  # 3dp refused
    with pytest.raises(ValueError):
        pp.usd_to_cents("abc")
    with pytest.raises(ValueError):
        pp.usd_to_cents("")
    with pytest.raises(ValueError):
        pp.usd_to_cents("10.9a")


def test_cents_to_usd_str():
    assert pp.cents_to_usd_str(1099) == "10.99"
    assert pp.cents_to_usd_str(1000) == "10.00"
    assert pp.cents_to_usd_str(1) == "0.01"
    assert pp.cents_to_usd_str(0) == "0.00"


def test_quote_fiat_bridge_math():
    q = pp.quote_fiat_bridge(10_000)  # $100.00
    assert q["usd_cents"] == 10_000
    assert q["fee_cents"] == 50  # 0.5% free tier
    assert q["fee_status"] == "free"
    assert q["net_cents"] == 9_950
    assert q["usdc_micro"] == 99_500_000  # 6dp
    assert q["rate"] == "1 USD = 1 USDC"
    with pytest.raises(ValueError):
        pp.quote_fiat_bridge(0)
    with pytest.raises(ValueError):
        pp.quote_fiat_bridge(-5)


# ---------------------------------------------------------------- (b) sandbox law
def test_sandbox_base_accepted():
    assert pp.assert_sandbox_base(SANDBOX) == SANDBOX


def test_production_base_hard_refused():
    with pytest.raises(RuntimeError, match="sandbox-only"):
        pp.assert_sandbox_base(PROD)


def test_unknown_base_refused():
    with pytest.raises(RuntimeError, match="unknown PayPal base"):
        pp.assert_sandbox_base("https://evil.example.com")


def test_real_transport_would_refuse_production():
    # The default transport enforces the sandbox prefix before any I/O.
    with pytest.raises(RuntimeError, match="sandbox-only"):
        pp._real_transport("GET", PROD + "/v1/oauth2/token", {}, None)


# ---------------------------------------------------------------- (c) creds
def test_missing_credentials_clean_error(monkeypatch):
    monkeypatch.delenv("PAYPAL_CLIENT_ID", raising=False)
    monkeypatch.delenv("PAYPAL_CLIENT_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="PAYPAL_CLIENT_ID"):
        pp.get_paypal_credentials()


# ---------------------------------------------------------------- (d) mocked API
def test_oauth_token_and_caching():
    a, t = _adapter(_token_route())
    tok1 = a.get_access_token()
    tok2 = a.get_access_token()
    assert tok1 == "tok-abc" == tok2
    assert len(t.calls_to("/v1/oauth2/token")) == 1  # cached
    # Basic auth header present, secret never in the URL or body
    _, url, headers, body = t.calls_to("/v1/oauth2/token")[0]
    assert headers["Authorization"].startswith("Basic ")
    assert "test-client-secret" not in url


def test_oauth_401_clean_error():
    a, t = _adapter({"/v1/oauth2/token": (401, {"error": "invalid_client"})})
    with pytest.raises(RuntimeError, match="auth failed"):
        a.get_access_token()


def _order_routes(**over):
    routes = _token_route()
    body = {"id": "ORDER-1", "status": "CREATED",
            "links": [{"rel": "approve",
                       "href": "https://sandbox.paypal.com/checkoutnow?token=ORDER-1"}]}
    body.update(over)
    routes["/v2/checkout/orders"] = (201, body)
    return routes


def test_create_order_happy_path():
    a, t = _adapter(_order_routes())
    out = a.create_order(1099, idempotency_key="idem-1")
    assert out["order_id"] == "ORDER-1"
    assert out["approve_url"].startswith("https://sandbox.paypal.com/")
    assert out["amount_cents"] == 1099
    posts = t.calls_to("/v2/checkout/orders")
    assert len(posts) == 1
    method, url, headers, payload = posts[0]
    assert headers["PayPal-Request-Id"] == "idem-1"
    assert payload["purchase_units"][0]["amount"] == {
        "currency_code": "USD", "value": "10.99"}


def test_create_order_bad_amount():
    a, _t = _adapter(_order_routes())
    with pytest.raises(ValueError):
        a.create_order(0)
    with pytest.raises(ValueError):
        a.create_order(-100)


def test_create_order_422_insufficient_funds():
    routes = _token_route()
    routes["/v2/checkout/orders"] = (
        422, {"details": [{"issue": "INSUFFICIENT_FUNDS",
                           "description": "nope"}]})
    a, _t = _adapter(routes)
    with pytest.raises(RuntimeError, match="insufficient"):
        a.create_order(100)


def test_get_order():
    routes = _token_route()
    routes["/v2/checkout/orders/ORDER-1"] = (
        200, {"id": "ORDER-1", "status": "APPROVED",
              "purchase_units": [{"amount": {"currency_code": "USD",
                                             "value": "10.99"}}]})
    a, _t = _adapter(routes)
    out = a.get_order("ORDER-1")
    assert out["status"] == "APPROVED"
    assert out["amount_cents"] == 1099
    with pytest.raises(ValueError):
        a.get_order("")


def test_capture_order_gated():
    a, _t = _adapter(_token_route())
    with pytest.raises(RuntimeError, match="AWL_PAYPAL_PAYOUTS"):
        a.capture_order("ORDER-1")


def test_capture_order_happy_path(monkeypatch):
    monkeypatch.setenv("AWL_PAYPAL_PAYOUTS", "1")
    routes = _token_route()
    routes["/v2/checkout/orders/ORDER-1/capture"] = (
        201, {"id": "ORDER-1", "status": "COMPLETED",
              "purchase_units": [{
                  "amount": {"currency_code": "USD", "value": "10.99"},
                  "payments": {"captures": [
                      {"id": "CAP-9",
                       "amount": {"currency_code": "USD",
                                  "value": "10.99"}}]}}]})
    a, t = _adapter(routes)
    out = a.capture_order("ORDER-1", idempotency_key="cap-1")
    assert out["status"] == "COMPLETED"
    assert out["capture_id"] == "CAP-9"
    assert out["amount_cents"] == 1099
    posts = t.calls_to("/capture")
    assert posts[0][2]["PayPal-Request-Id"] == "cap-1"


def test_create_payout_gated():
    a, _t = _adapter(_token_route())
    with pytest.raises(RuntimeError, match="AWL_PAYPAL_PAYOUTS"):
        a.create_payout("agent@example.com", 100)


def test_create_payout_happy_path(monkeypatch):
    monkeypatch.setenv("AWL_PAYPAL_PAYOUTS", "1")
    routes = _token_route()
    routes["/v1/payments/payouts"] = (
        201, {"batch_header": {"payout_batch_id": "BATCH-7",
                               "batch_status": "PENDING"}})
    a, t = _adapter(routes)
    out = a.create_payout("agent@example.com", 250,
                          note="demo", idempotency_key="pay-1")
    assert out["batch_id"] == "BATCH-7"
    assert out["batch_status"] == "PENDING"
    posts = [c for c in t.calls if c[1].endswith("/v1/payments/payouts")
             and c[0] == "POST"]
    assert len(posts) == 1
    _, _, headers, payload = posts[0]
    assert headers["PayPal-Request-Id"] == "pay-1"
    item = payload["items"][0]
    assert item["receiver"] == "agent@example.com"
    assert item["amount"] == {"value": "2.50", "currency": "USD"}
    with pytest.raises(ValueError):
        a.create_payout("not-an-email", 100)
    with pytest.raises(ValueError):
        a.create_payout("a@b.c", 0)


def test_create_payout_403_scope_error(monkeypatch):
    monkeypatch.setenv("AWL_PAYPAL_PAYOUTS", "1")
    routes = _token_route()
    routes["/v1/payments/payouts"] = (
        403, {"message": "Not authorized"})
    a, _t = _adapter(routes)
    with pytest.raises(RuntimeError, match="scope"):
        a.create_payout("agent@example.com", 100)


def test_get_payout():
    routes = _token_route()
    routes["/v1/payments/payouts/BATCH-7"] = (
        200, {"batch_header": {"payout_batch_id": "BATCH-7",
                               "batch_status": "SUCCESS"}})
    a, _t = _adapter(routes)
    out = a.get_payout("BATCH-7")
    assert out["batch_status"] == "SUCCESS"


# ---------------------------------------------------------------- (f) router
def test_router_fiat_edge_resolves():
    o = MockOracle({("paypal", "USD"): 1.0, ("base", "USDC"): 1.0})
    p = ConverterRouter().find_path("paypal", "USD", "base", "USDC", o)
    assert p == [
        {"chain": "paypal", "token": "USD", "hop": "origin"},
        {"chain": "base", "token": "USDC", "hop": "fiat"},
    ]


def test_router_paths_through_fiat():
    o = MockOracle({("paypal", "USD"): 1.0, ("base", "USDC"): 1.0,
                    ("base", "ETH"): 4000.0})
    p = ConverterRouter().find_path("paypal", "USD", "base", "ETH", o)
    assert p is not None
    assert [s["hop"] for s in p] == ["origin", "fiat", "swap"]
    assert [s["token"] for s in p] == ["USD", "USDC", "ETH"]


def test_router_unpriced_usd_refuses():
    o = MockOracle({("base", "USDC"): 1.0})  # USD unpriced
    assert ConverterRouter().find_path(
        "paypal", "USD", "base", "USDC", o) is None


def test_router_existing_paths_unchanged():
    o = default_mock_oracle()  # now prices ("paypal","USD") too
    r = ConverterRouter()
    p = r.find_path("ethereum", "ETH", "base", "USDC", o)
    assert [s["hop"] for s in p] == ["origin", "swap", "bridge"]
    p2 = r.find_path("solana", "SOL", "ethereum", "ETH", o)
    assert [s["token"] for s in p2] == ["SOL", "USDC", "USDC", "ETH"]
    assert CoinGeckoOracle().has_value("paypal", "USD")  # unit of account


# ---------------------------------------------------------------- (g) receipts
def test_bridge_receipt_signs_and_verifies():
    sk = SigningKey.generate()
    r = pp.bridge_receipt("payout", "BATCH-7", 250, "out")
    assert r["rail"] == "paypal" and r["sandbox"] is True
    assert r["currency"] == "USD"
    env = pp.sign_bridge_receipt(r, sk)
    assert chamber.verify_attestation(env["attestation"], sk.verify_key)


def test_bridge_receipt_tamper_fails():
    sk = SigningKey.generate()
    env = pp.sign_bridge_receipt(
        pp.bridge_receipt("capture", "CAP-9", 1099, "in"), sk)
    tampered = dict(env["attestation"])
    import json as _json
    payload = _json.loads(tampered["payload"])
    payload["amount_cents"] = 999_999
    tampered["payload"] = _json.dumps(payload, sort_keys=True,
                                      separators=(",", ":"))
    assert not chamber.verify_attestation(tampered, sk.verify_key)
    other = SigningKey.generate()
    assert not chamber.verify_attestation(env["attestation"],
                                          other.verify_key)


def test_bridge_receipt_validation():
    with pytest.raises(ValueError):
        pp.bridge_receipt("bogus", "X", 1, "out")
    with pytest.raises(ValueError):
        pp.bridge_receipt("payout", "X", 1, "sideways")
