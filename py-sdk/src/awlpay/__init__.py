"""awlpay — the 5-minute stablecoin wallet for agents.

Testnet by default. No seed phrases, no gas math, no rail selection.
"""

from .errors import (
    AwlPayError,
    InsufficientFunds,
    MainnetConfirmationError,
    RailError,
    SpendCapExceeded,
    UnsupportedRail,
)
from .store import WrongPassword
from .wallet import MAINNET_CONFIRMATION, AgentWallet

__version__ = "0.1.0"

__all__ = [
    "AgentWallet",
    "AwlPayError",
    "InsufficientFunds",
    "MainnetConfirmationError",
    "RailError",
    "SpendCapExceeded",
    "UnsupportedRail",
    "WrongPassword",
    "MAINNET_CONFIRMATION",
    "__version__",
]
