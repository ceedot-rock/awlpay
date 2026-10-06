"""awLPay pricing tests (workstream 3): cost-plus route toll + l33t fair-use cap.

Covers:
  - route_price_cents(): the locked 1c toll, the cost-plus formula's
    integer math (ceil, margin scaling, 1c floor), invalid-input refusal,
    and replay of every spec/fixtures/route_price.json fixture.
  - Tier boundaries the fee law must hold: pro volume/tx cap edges and the
    overage fallback to EXACT free pricing (incl. floor division), and the
    l33t fair-use guard edges at the econ-derived 127,669 cap.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import fees  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NC = fees.NANOCENTS_PER_CENT  # 1_000_000_000


def _route_fixtures():
    with open(os.path.join(ROOT, "spec", "fixtures",
                           "route_price.json"), encoding="utf-8") as fh:
        return json.load(fh)


_ROUTE_FIXTURES = _route_fixtures()


# ---------------------------------------------------------------- route toll
def test_route_price_locked_value():
    # The toll the 402 gate charges: cost-plus evaluates to 1c.
    assert fees.route_price_cents() == 1
    assert fees.ROUTE_PRICE_CENTS == 1


def test_route_price_formula_breakdown():
    # The locked constants behind the 1c, each labeled.
    assert fees.ROUTE_COMPUTE_NC == 172          # 2.23ms @ Fly shared-1x
    assert fees.ROUTE_RPC_NC == 1_440_000        # 32 CU @ Alchemy PAYG $0.45/M
    assert fees.ROUTE_ORACLE_NC == 0             # 60s cache amortizes to ~0
    assert (fees.ROUTE_MARGIN_NUM, fees.ROUTE_MARGIN_DEN) == (4, 1)
    unit = 172 + 1_440_000 + 0
    assert fees.route_price_cents(unit, 0, 0, 4, 1) == 1
    # ... and with the pieces separated the same way:
    assert fees.route_price_cents(172, 1_440_000, 0, 4, 1) == 1


def test_route_price_ceil_not_floor():
    # 1,000,000,001 nc is a hair over 1c: ceil -> 2, never 1.
    assert fees.route_price_cents(1_000_000_001, 0, 0, 1, 1) == 2
    # Exactly 1c stays 1c.
    assert fees.route_price_cents(1_000_000_000, 0, 0, 1, 1) == 1


def test_route_price_margin_scales():
    # Margin is a num/den pair, applied exactly.
    assert fees.route_price_cents(2 * NC, 0, 0, 3, 2) == 3   # 2c * 3/2
    assert fees.route_price_cents(2 * NC, 0, 0, 1, 1) == 2
    assert fees.route_price_cents(0, 0, 0, 100, 1) == 1      # floor binds


def test_route_price_floor_binds():
    # Any computed price below 1c becomes the 1c minimum toll.
    assert fees.route_price_cents(0, 0, 0, 4, 1) == 1
    assert fees.route_price_cents(1, 0, 0, 1, 1) == 1
    assert fees.route_price_cents(999_999_999, 0, 0, 1, 1) == 1


def test_route_price_integer_only():
    # No floats anywhere: huge inputs stay exact ints.
    p = fees.route_price_cents(10**18, 10**18, 10**18, 7, 3)
    assert isinstance(p, int)
    # (3e18 * 7/3) = 7e18 nc = 7,000,000,000 c
    assert p == 7_000_000_000


def test_route_price_rejects_bad_inputs():
    with pytest.raises(ValueError):
        fees.route_price_cents(-1, 0, 0, 4, 1)
    with pytest.raises(ValueError):
        fees.route_price_cents(0, -5, 0, 4, 1)
    with pytest.raises(ValueError):
        fees.route_price_cents(0, 0, 0, 4, 0)   # margin_den <= 0
    with pytest.raises(ValueError):
        fees.route_price_cents(0, 0, 0, -1, 1)  # negative margin
    with pytest.raises(TypeError):
        fees.route_price_cents(1.5, 0, 0, 4, 1)


def test_route_price_fixture_count():
    assert len(_ROUTE_FIXTURES) == 8, (
        "route_price.json shrank: %d fixtures" % len(_ROUTE_FIXTURES))


@pytest.mark.parametrize(
    "fx", _ROUTE_FIXTURES,
    ids=["route_%d" % f["price_cents"] + "_%d" % i
         for i, f in enumerate(_ROUTE_FIXTURES)])
def test_route_price_fixture_replay(fx):
    got = fees.route_price_cents(fx["compute_nc"], fx["rpc_nc"],
                                 fx["oracle_nc"], fx["margin_num"],
                                 fx["margin_den"])
    assert got == fx["price_cents"], (
        "route price mismatch on fixture %r: python=%d spec=%d"
        % (fx.get("note"), got, fx["price_cents"]))
    assert isinstance(got, int)


# ------------------------------------------------- tier caps and overage
def test_pro_cap_boundaries():
    cap_v, cap_t = fees.PRO_VOLUME_CAP_CENTS, fees.PRO_TX_CAP
    # Volume edge is inclusive: volume + amount <= 3,000,000c stays under.
    assert fees.calculate_fees(1, 1, cap_v - 1, 0) == (0, "pro_under_cap")
    assert fees.calculate_fees(1, 1, cap_v, 0)[1] == "pro_overage"
    # Tx edge is exclusive: txs_used < 500.
    assert fees.calculate_fees(1, 1, 0, cap_t - 1) == (0, "pro_under_cap")
    assert fees.calculate_fees(1, 1, 0, cap_t)[1] == "pro_overage"


def test_pro_overage_falls_back_to_free_pricing_exactly():
    # Overage IS free pricing: 0.5% floored, on the same amounts.
    for amt in (1, 99, 100, 101, 333, 10_000, 999_999):
        over_fee, over_status = fees.calculate_fees(amt, 1, 3_000_000, 0)
        free_fee, _ = fees.calculate_fees(amt, 0, 0, 0)
        assert over_status == "pro_overage"
        assert over_fee == free_fee == amt // 200
    # ... and via the tx-count cap too.
    over_fee, over_status = fees.calculate_fees(10_000, 1, 0, 500)
    assert (over_fee, over_status) == (50, "pro_overage")


def test_l33t_fair_use_cap_boundaries():
    cap = fees.L33T_TX_FAIR_USE
    assert cap == 127_669  # econ-derived; PRICING.md has the math
    assert fees.calculate_fees(10_000, 2, 0, cap - 1) == (0, "l33t")
    assert fees.calculate_fees(10_000, 2, 0, cap) == (0, "l33t")
    assert fees.calculate_fees(10_000, 2, 0, cap + 1) == (
        0, "l33t_fair_use_review")


def test_l33t_guard_is_tx_count_only():
    # Volume is irrelevant to l33t: the guard counts transactions.
    assert fees.calculate_fees(10_000, 2, 10**12, 0) == (0, "l33t")
    assert fees.calculate_fees(10_000, 2, 10**12,
                               fees.L33T_TX_FAIR_USE + 1) == (
        0, "l33t_fair_use_review")
    # The flag is honest, never a charge: fee stays 0 past the cap.
    fee, _ = fees.calculate_fees(10_000, 2, 0, 10**9)
    assert fee == 0


def test_free_tier_2026_10_06():
    # Corey 2026-10-06: Free cut to 0.5% flat, no fixed fee —
    # undercuts Phantom 0.85% / MetaMask 0.875% at every size.
    assert fees.calculate_fees(10_000, 0, 0, 0) == (50, "free")
    assert fees.calculate_fees(0, 0, 0, 0) == (0, "free")
    assert fees.FREE_DIVISOR == 200
