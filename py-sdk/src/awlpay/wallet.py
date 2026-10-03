"""AgentWallet — the 5-minute stablecoin wallet for agents.

.. code-block:: python

    from awlpay import AgentWallet

    w = AgentWallet(password="correct horse battery staple")  # testnet by default
    print(w.deposit_address("base"))   # fund this address with USDC (testnet)
    print(w.balances())                # {"base": 0.0, "solana": 0.0}
    receipt = w.pay("0xRecipient...", 1.0)  # auto-picks the cheapest funded rail

Safety model, in one paragraph: the wallet file is AES-256-GCM encrypted
with a key derived from your password (never stored, never logged);
testnet is the default and mainnet requires the literal typed
confirmation ``"I UNDERSTAND"``; every wallet carries a lifetime spend
cap (default $100) plus an optional per-call cap, and plain transfers
never touch the AwLPay server — they go straight to chain RPC.
"""

from __future__ import annotations

import datetime
from pathlib import Path

from . import rails as _rails_module
from .errors import InsufficientFunds, MainnetConfirmationError, SpendCapExceeded
from .store import WALLET_FILE, WrongPassword, load_wallet, save_wallet, wallet_exists

MAINNET_CONFIRMATION = "I UNDERSTAND"


class AgentWallet:
    """A password-encrypted multi-rail stablecoin wallet for agents."""

    def __init__(
        self,
        password: str,
        network: str = "testnet",
        max_spend_usd: float = 100.0,
        confirm_mainnet: str | None = None,
        _store_path: Path | None = None,
        _rails: dict | None = None,
    ):
        if network not in ("testnet", "mainnet"):
            raise ValueError("network must be 'testnet' or 'mainnet'")
        if network == "mainnet" and confirm_mainnet != MAINNET_CONFIRMATION:
            raise MainnetConfirmationError(
                "Mainnet moves real money. To enable it, construct the wallet "
                "with network=\"mainnet\" and confirm_mainnet=\"I UNDERSTAND\" "
                "(the literal string). Testnet is the default."
            )
        if max_spend_usd <= 0:
            raise ValueError("max_spend_usd must be positive")

        self._password = password
        self.network = network
        self.max_spend_usd = float(max_spend_usd)
        self._store_path = _store_path or WALLET_FILE

        if _rails is not None:
            self._rail_adapters = _rails
        else:
            self._rail_adapters = {
                name: _rails_module.make_rail(name, network)
                for name in _rails_module.RAILS
            }

        if wallet_exists(self._store_path):
            try:
                data = load_wallet(password, self._store_path)
            except WrongPassword:
                raise
            if data.get("network") != network:
                raise ValueError(
                    f"This wallet file was created for {data.get('network')!r}; "
                    f"you asked for {network!r}. Use a fresh AWLPAY_HOME or "
                    "move the file aside."
                )
            self._data = data
        else:
            self._data = {
                "version": 1,
                "network": network,
                "rails": {
                    name: adapter.new_keypair()
                    for name, adapter in self._rail_adapters.items()
                },
                "lifetime_spent_usd": 0.0,
            }
            self._persist()

    # -- internal ---------------------------------------------------------
    def _persist(self) -> None:
        save_wallet(self._data, self._password, self._store_path)

    def _adapter(self, rail: str):
        try:
            return self._rail_adapters[rail]
        except KeyError:
            from .errors import UnsupportedRail

            raise UnsupportedRail(
                f"Unknown rail {rail!r}. Supported: {sorted(self._rail_adapters)}"
            ) from None

    # -- public API --------------------------------------------------------
    @property
    def rails(self) -> list[str]:
        """Rail names this wallet can pay on, e.g. ["base", "solana"]."""
        return sorted(self._rail_adapters)

    def deposit_address(self, rail: str) -> str:
        """The address to fund on ``rail`` ("base" | "solana")."""
        adapter = self._adapter(rail)
        entry = self._data["rails"].get(rail)
        if entry is None:  # rail added after wallet creation
            entry = adapter.new_keypair()
            self._data["rails"][rail] = entry
            self._persist()
        return entry["address"]

    def balances(self) -> dict[str, float]:
        """USDC balance per rail, in USD. 1 USDC == 1 USD by definition."""
        out: dict[str, float] = {}
        for name, adapter in self._rail_adapters.items():
            out[name] = round(adapter.balance_usd(self.deposit_address(name)), 6)
        return out

    def lifetime_spent_usd(self) -> float:
        """Total USD this wallet has paid out (locally tracked, all time)."""
        return float(self._data.get("lifetime_spent_usd", 0.0))

    def _choose_rail(self, usd: float, rail: str) -> str:
        if rail != "auto":
            return rail
        candidates = []
        for name, adapter in self._rail_adapters.items():
            bal = adapter.balance_usd(self.deposit_address(name))
            if bal >= usd:
                candidates.append((adapter.fee_estimate_usd, name))
        if not candidates:
            raise InsufficientFunds(
                f"No rail holds at least ${usd:.2f}. Fund one of these "
                f"addresses first: "
                + ", ".join(
                    f"{n}={self.deposit_address(n)}" for n in self._rail_adapters
                ),
                balances=self.balances(),
            )
        candidates.sort()
        return candidates[0][1]

    def pay(
        self,
        to_address: str,
        usd: float,
        rail: str = "auto",
        max_usd: float | None = None,
    ) -> dict:
        """Pay ``usd`` dollars of USDC to ``to_address``.

        ``rail`` is "base", "solana", or "auto" (default: cheapest FUNDED
        rail). ``max_usd`` is an optional per-call cap. Returns a receipt::

            {"rail": "solana", "tx_hash": "...", "usd": 1.0,
             "to": "...", "confirmed_at": "2026-10-03T...", "network": "testnet"}
        """
        if usd <= 0:
            raise ValueError("usd must be positive")
        if max_usd is not None and usd > max_usd:
            raise SpendCapExceeded(
                f"Payment of ${usd:.2f} exceeds the per-call cap of "
                f"${max_usd:.2f}."
            )
        spent = self.lifetime_spent_usd()
        if spent + usd > self.max_spend_usd:
            raise SpendCapExceeded(
                f"Payment of ${usd:.2f} would exceed the lifetime cap of "
                f"${self.max_spend_usd:.2f} (already spent ${spent:.2f})."
            )

        chosen = self._choose_rail(usd, rail)
        adapter = self._adapter(chosen)
        entry = self._data["rails"][chosen]
        if adapter.balance_usd(entry["address"]) < usd:
            raise InsufficientFunds(
                f"Rail {chosen!r} holds less than ${usd:.2f}.",
                balances=self.balances(),
            )

        tx_hash = adapter.pay(entry["private_key"], to_address, usd)

        # Only count confirmed payments against the cap.
        self._data["lifetime_spent_usd"] = round(spent + usd, 6)
        self._persist()

        return {
            "rail": chosen,
            "tx_hash": tx_hash,
            "usd": usd,
            "to": to_address,
            "confirmed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "network": self.network,
        }
