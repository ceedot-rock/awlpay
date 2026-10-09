"""awLPay price oracle: hasValue() — does this token have verifiable value?

=====================================================================
TRUST ASSUMPTION (READ BEFORE DEPLOYING)
---------------------------------------------------------------------
CoinGeckoOracle below is a CENTRALIZED, CENSORABLE price feed. Relying
on it means trusting CoinGecko's API responses: they can be delayed,
wrong, rate-limited, or censored, and whoever controls the feed can
censor which tokens "have value". The PriceOracle ABC exists precisely
so a trust-minimized oracle (e.g. on-chain TWAP / light-client proofs)
can be swapped in later without touching the router, fees, or
settlement code. v1 is pragmatic: CoinGecko behind a strict timeout,
and ANY failure degrades to None (unpriced) rather than a guess.
=====================================================================

Local law: hasValue(token) is true iff get_price_usd() returns a float.
An unpriced token refuses the quote — never guess, never zero-price.
"""

from __future__ import annotations

import json
import urllib.request
from abc import ABC, abstractmethod


class PriceOracle(ABC):
    """Abstract price source. All prices are USD per whole token."""

    @abstractmethod
    def get_price_usd(self, chain: str, token: str) -> float | None:
        """Return USD price, or None if the token has no verifiable price."""
        raise NotImplementedError

    def has_value(self, chain: str, token: str) -> bool:
        return self.get_price_usd(chain, token) is not None


# Map (chain, token) -> CoinGecko coin id. Only tokens with deep,
# long-lived markets are listed here; anything else is unpriced.
_COINGECKO_IDS = {
    ("ethereum", "ETH"): "ethereum",
    ("ethereum", "USDC"): "usd-coin",
    ("base", "ETH"): "ethereum",
    ("base", "USDC"): "usd-coin",
    ("polygon", "ETH"): "ethereum",
    ("polygon", "USDC"): "usd-coin",
    ("arbitrum", "ETH"): "ethereum",
    ("arbitrum", "USDC"): "usd-coin",
    ("solana", "SOL"): "solana",
    ("solana", "USDC"): "usd-coin",
    ("xrpl", "XRP"): "ripple",
}

_COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price"
_TIMEOUT_S = 6  # short: a slow feed is a failed feed


class CoinGeckoOracle(PriceOracle):
    """Live prices from CoinGecko's free API.

    Failures of ANY kind (network, timeout, bad JSON, missing field)
    return None — never raise, never fabricate a price. See the trust
    assumption at the top of this file.

    Exception: ("paypal", "USD") is the unit of account — 1.0 by
    definition, not a market price — so it never consults the feed.
    """

    def __init__(self, timeout: float = _TIMEOUT_S):
        self.timeout = timeout

    def get_price_usd(self, chain: str, token: str) -> float | None:
        if chain.lower() == "paypal" and token.upper() == "USD":
            return 1.0  # unit of account: definitional, not market
        coin_id = _COINGECKO_IDS.get((chain.lower(), token.upper()))
        if coin_id is None:
            return None
        try:
            url = "%s?ids=%s&vs_currencies=usd" % (_COINGECKO_URL, coin_id)
            req = urllib.request.Request(
                url, headers={"User-Agent": "awlpay/1.0", "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                data = json.load(r)
            price = data.get(coin_id, {}).get("usd")
            if isinstance(price, (int, float)) and price > 0:
                return float(price)
            return None
        except Exception:
            # Any failure (timeout, DNS, HTTP error, bad JSON) -> unpriced.
            return None


class MockOracle(PriceOracle):
    """Deterministic price map for tests and offline runs.

    prices: {(chain, token): usd_float}. Lookup is case-insensitive on
    both parts; anything absent is unpriced (None).
    """

    def __init__(self, prices: dict[tuple[str, str], float]):
        self._prices = {(str(c).lower(), str(t).upper()): float(p)
                        for (c, t), p in prices.items()}

    def get_price_usd(self, chain: str, token: str) -> float | None:
        return self._prices.get((str(chain).lower(), str(token).upper()))


def default_mock_oracle() -> MockOracle:
    """Sane offline default prices used by the server when AWL_ORACLE
    is not set to 'coingecko'."""
    prices: dict[tuple[str, str], float] = {}
    for chain in ("ethereum", "base", "polygon", "arbitrum"):
        prices[(chain, "ETH")] = 4000.0
        prices[(chain, "USDC")] = 1.0
    prices[("solana", "SOL")] = 150.0
    prices[("solana", "USDC")] = 1.0
    prices[("xrpl", "XRP")] = 2.0
    prices[("paypal", "USD")] = 1.0  # unit of account (fiat bridge)
    return MockOracle(prices)
