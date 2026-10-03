"""awlpay — errors for the AgentWallet SDK."""


class AwlPayError(Exception):
    """Base class for all awlpay SDK errors."""


class SpendCapExceeded(AwlPayError):
    """A payment was refused because it exceeds the per-call or lifetime cap."""


class InsufficientFunds(AwlPayError):
    """No funded rail can cover the requested amount.

    Carries ``balances`` — the USD balance seen on each rail — so the
    caller can tell the user exactly where to fund.
    """

    def __init__(self, message: str, balances: dict | None = None):
        super().__init__(message)
        self.balances = balances or {}


class MainnetConfirmationError(AwlPayError):
    """Mainnet was requested without the explicit typed confirmation."""


class UnsupportedRail(AwlPayError):
    """An unknown rail name was requested."""


class RailError(AwlPayError):
    """A rail's RPC endpoint failed (flaky network, rate limit, downtime).

    Your money is safe — this is raised before anything is signed.
    Retry the call; public testnet endpoints are the usual culprit.
    """
