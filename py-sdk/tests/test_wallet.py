"""Offline unit tests for the awlpay Python SDK.

No network, no real funds, no mainnet. Rails are faked; the wallet
logic (encryption, caps, rail selection, receipts) is what's under test.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from awlpay import (
    AgentWallet,
    InsufficientFunds,
    MainnetConfirmationError,
    SpendCapExceeded,
    UnsupportedRail,
)
from awlpay.store import WrongPassword, load_wallet, save_wallet


class FakeRail:
    """Scripted rail: canned balance, records pay() calls."""

    def __init__(self, name, balance=0.0, fee=0.01):
        self.name = name
        self._balance = balance
        self._fee = fee
        self.pay_calls: list[tuple[str, float]] = []

    @property
    def fee_estimate_usd(self):
        return self._fee

    def new_keypair(self):
        return {"private_key": "00" * 32, "address": f"{self.name}-addr-1"}

    def address_from_key(self, _pk):
        return f"{self.name}-addr-1"

    def balance_usd(self, _address):
        return self._balance

    def pay(self, _private_key_hex, to_address, usd):
        self.pay_calls.append((to_address, usd))
        return f"fake-tx-{self.name}-{len(self.pay_calls)}"


def make_wallet(tmp_path, password="pw", rails=None, **kw):
    rails = rails or {
        "base": FakeRail("base", balance=0.0, fee=0.02),
        "solana": FakeRail("solana", balance=0.0, fee=0.0003),
    }
    return AgentWallet(
        password, _store_path=tmp_path / "wallets.enc", _rails=rails, **kw
    )


# -- store ---------------------------------------------------------------

def test_encrypt_decrypt_roundtrip(tmp_path):
    p = tmp_path / "w.enc"
    save_wallet({"hello": "world", "n": 3}, "s3cret", p)
    assert load_wallet("s3cret", p) == {"hello": "world", "n": 3}
    # locked-down permissions
    assert p.stat().st_mode & 0o777 == 0o600


def test_wrong_password_raises(tmp_path):
    p = tmp_path / "w.enc"
    save_wallet({"a": 1}, "right", p)
    with pytest.raises(WrongPassword):
        load_wallet("wrong", p)


# -- wallet lifecycle ----------------------------------------------------

def test_first_run_generates_and_persists(tmp_path):
    w = make_wallet(tmp_path)
    assert w.network == "testnet"
    assert w.rails == ["base", "solana"]
    assert w.lifetime_spent_usd() == 0.0
    # second construction with same password loads the same wallet
    w2 = make_wallet(tmp_path)
    assert w2.deposit_address("base") == w.deposit_address("base")
    assert w2.deposit_address("solana") == w.deposit_address("solana")


def test_wrong_password_on_existing_wallet(tmp_path):
    make_wallet(tmp_path, password="right")
    with pytest.raises(WrongPassword):
        make_wallet(tmp_path, password="wrong")


def test_unknown_rail(tmp_path):
    w = make_wallet(tmp_path)
    with pytest.raises(UnsupportedRail):
        w.deposit_address("doge")


def test_balances_shape(tmp_path):
    rails = {"base": FakeRail("base", 2.5), "solana": FakeRail("solana", 1.25)}
    w = make_wallet(tmp_path, rails=rails)
    assert w.balances() == {"base": 2.5, "solana": 1.25}


# -- spend caps ----------------------------------------------------------

def test_per_call_cap_refuses_before_signing(tmp_path):
    rails = {"base": FakeRail("base", balance=100.0)}
    w = make_wallet(tmp_path, rails=rails)
    with pytest.raises(SpendCapExceeded):
        w.pay("0xabc", 10.0, rail="base", max_usd=5.0)
    assert rails["base"].pay_calls == []  # nothing was signed


def test_lifetime_cap_refuses_before_signing(tmp_path):
    rails = {"base": FakeRail("base", balance=100.0)}
    w = make_wallet(tmp_path, rails=rails, max_spend_usd=10.0)
    w.pay("0xabc", 6.0, rail="base")
    assert w.lifetime_spent_usd() == 6.0
    with pytest.raises(SpendCapExceeded):
        w.pay("0xabc", 5.0, rail="base")
    assert len(rails["base"].pay_calls) == 1  # second never sent


def test_lifetime_spent_persists(tmp_path):
    rails = {"base": FakeRail("base", balance=100.0)}
    w = make_wallet(tmp_path, rails=rails)
    w.pay("0xabc", 3.0, rail="base")
    w2 = make_wallet(tmp_path, rails={"base": FakeRail("base", balance=100.0)})
    assert w2.lifetime_spent_usd() == 3.0


def test_nonpositive_amount_rejected(tmp_path):
    w = make_wallet(tmp_path)
    with pytest.raises(ValueError):
        w.pay("0xabc", 0, rail="base")


# -- rail selection ------------------------------------------------------

def test_auto_picks_cheapest_funded(tmp_path):
    rails = {
        "base": FakeRail("base", balance=50.0, fee=0.02),
        "solana": FakeRail("solana", balance=50.0, fee=0.0003),
    }
    w = make_wallet(tmp_path, rails=rails)
    receipt = w.pay("dest", 1.0)  # rail="auto"
    assert receipt["rail"] == "solana"
    assert rails["solana"].pay_calls == [("dest", 1.0)]
    assert rails["base"].pay_calls == []


def test_auto_falls_back_to_funded_rail(tmp_path):
    rails = {
        "base": FakeRail("base", balance=50.0, fee=0.02),
        "solana": FakeRail("solana", balance=0.0, fee=0.0003),
    }
    w = make_wallet(tmp_path, rails=rails)
    receipt = w.pay("dest", 1.0)
    assert receipt["rail"] == "base"


def test_auto_no_funds_raises_with_balances(tmp_path):
    rails = {
        "base": FakeRail("base", balance=0.0),
        "solana": FakeRail("solana", balance=0.0),
    }
    w = make_wallet(tmp_path, rails=rails)
    with pytest.raises(InsufficientFunds) as ei:
        w.pay("dest", 1.0)
    assert ei.value.balances == {"base": 0.0, "solana": 0.0}
    assert "fund" in str(ei.value).lower()


def test_explicit_rail_insufficient_raises(tmp_path):
    rails = {"base": FakeRail("base", balance=0.5)}
    w = make_wallet(tmp_path, rails=rails)
    with pytest.raises(InsufficientFunds):
        w.pay("dest", 1.0, rail="base")


# -- receipt -------------------------------------------------------------

def test_receipt_shape(tmp_path):
    rails = {"solana": FakeRail("solana", balance=10.0)}
    w = make_wallet(tmp_path, rails=rails)
    r = w.pay("dest-addr", 2.5, rail="solana")
    assert r["rail"] == "solana"
    assert r["tx_hash"] == "fake-tx-solana-1"
    assert r["usd"] == 2.5
    assert r["to"] == "dest-addr"
    assert r["network"] == "testnet"
    assert re.match(r"\d{4}-\d{2}-\d{2}T", r["confirmed_at"])


# -- network safety ------------------------------------------------------

def test_testnet_is_default(tmp_path):
    w = make_wallet(tmp_path)
    assert w.network == "testnet"


def test_mainnet_requires_exact_confirmation(tmp_path):
    with pytest.raises(MainnetConfirmationError):
        make_wallet(tmp_path, network="mainnet")
    with pytest.raises(MainnetConfirmationError):
        make_wallet(tmp_path, network="mainnet", confirm_mainnet="yes")
    with pytest.raises(MainnetConfirmationError):
        make_wallet(tmp_path, network="mainnet", confirm_mainnet="i understand")


def test_mainnet_accepts_exact_confirmation(tmp_path):
    rails = {"base": FakeRail("base", balance=10.0)}
    w = make_wallet(
        tmp_path, rails=rails, network="mainnet", confirm_mainnet="I UNDERSTAND"
    )
    assert w.network == "mainnet"
    r = w.pay("dest", 1.0, rail="base")
    assert r["network"] == "mainnet"


def test_bad_network_rejected(tmp_path):
    with pytest.raises(ValueError):
        make_wallet(tmp_path, network="devnet")


# -- rail RPC resilience -------------------------------------------------

def test_with_retry_wraps_transient_failures():
    from awlpay.errors import RailError
    from awlpay.rails import _with_retry

    calls = []

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise ConnectionError("boom")
        return "ok"

    assert _with_retry(flaky, "test op") == "ok"
    assert len(calls) == 2


def test_with_retry_gives_up_cleanly():
    from awlpay.errors import RailError
    from awlpay.rails import _with_retry

    def always_down():
        raise TimeoutError("nope")

    with pytest.raises(RailError) as ei:
        _with_retry(always_down, "test op")
    assert "test op" in str(ei.value)
