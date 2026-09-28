"""awLPay path executor: turns a ConverterRouter path into real chain ops.

Consumes router.find_path() output — a list of steps
    [{"chain", "token", "hop": "origin"|"bridge"|"swap"}, ...]
— and executes each consecutive pair as a hop.

v1 WIRING (honest): only same-chain same-token "transfer" hops execute
for real (EVM native + ERC-20 USDC via EvmAdapter; native SOL via
SolanaAdapter). "swap" and "bridge" hops have NO dex/bridge protocol
wired yet, so a path containing one REFUSES the whole settlement with
refused_unwired_hop BEFORE executing anything — atomic: this executor
never leaves half a path executed.

DUST LAW (mirrors spec/SettlementEngine.cuni applyFees): net_cents is
converted to integer base units with EXACT floor math
(fractions.Fraction — no floats). base_units == 0 ->
refused_dust_eaten_by_fees. net <= 0 is refused upstream, but the
executor re-checks (defense in depth).

STATUS CONTRACT: every refusal status starts with "refused_" per the
CuNi spec's status contract. The fee law itself (calculate_fees /
applyFees) is untouched — this module adds executor-layer refusals
only, so the Python/CuNi integer-cents agreement and the fixture
cross-checks stay green.

MODES: "dryrun" (build + sign + simulate, never broadcast) and
"broadcast" (gated: AWL_BROADCAST=1, testnet-only, explicit recipient).
Anything else -> ValueError. Broadcast is all-or-nothing per hop
sequence: a failed hop broadcast raises LOUDLY (funds may already be
in flight on chain — the error says so).
"""

from __future__ import annotations

import os
from fractions import Fraction

from .chains import broadcast_allowed, get_settler_seed

MODES = ("dryrun", "broadcast")


def cents_to_base_units(net_cents: int, decimals: int,
                        price_usd: float) -> int:
    """Floor(net_cents/100 / price_usd * 10**decimals), exact integer math.

    No floats: Fraction keeps the floor exact (this is the same
    integer-cents floor-division agreement the CuNi fee spec uses).
    Returns 0 when the net value doesn't cover one base unit (dust).
    """
    if net_cents <= 0 or price_usd is None or price_usd <= 0:
        return 0
    units = (Fraction(net_cents, 100) * (10 ** decimals)
             / Fraction(price_usd).limit_denominator(10 ** 12))
    return int(units)  # Fraction.__int__ floors for positives


def _classify_hop(prev: dict, cur: dict) -> str:
    if prev["chain"] == cur["chain"] and prev["token"] == cur["token"]:
        return "transfer"
    if prev["chain"] == cur["chain"]:
        return "swap"
    if prev["token"] == cur["token"]:
        return "bridge"
    return "unwired"


def _validate_recipient(chain: str, to_address: str, family: str) -> str:
    if family == "evm":
        addr = to_address.strip()
        if (len(addr) == 42 and addr.startswith("0x")
                and all(c in "0123456789abcdefABCDEF" for c in addr[2:])):
            return addr
        raise ValueError("bad EVM recipient address %r" % to_address)
    # solana: base58 pubkey parse is the validation
    from solders.pubkey import Pubkey
    try:
        return str(Pubkey.from_string(to_address.strip()))
    except Exception:
        raise ValueError("bad Solana recipient address %r" % to_address)


def _throwaway_recipient(chain: str, family: str) -> str:
    if family == "evm":
        from eth_account import Account
        return Account.create().address
    from solders.keypair import Keypair
    return str(Keypair().pubkey())


class PathExecutor:
    """Executes router paths hop-by-hop. See module docstring for law."""

    def __init__(self, adapters: dict, key_provider=get_settler_seed,
                 mode: str = "dryrun"):
        if mode not in MODES:
            raise ValueError("mode must be one of %s, got %r" % (MODES, mode))
        self.adapters = adapters
        self.key_provider = key_provider
        self.mode = mode

    def _refusal(self, status: str, detail: str, net_cents: int) -> dict:
        return {
            "refused": True,
            "status": status,
            "detail": detail,
            "mode": self.mode,
            "hops": [],
            "net_cents": net_cents,
        }

    def execute(self, path: list[dict], net_cents: int, oracle,
                to_address: str | None = None) -> dict:
        """Execute every hop of `path` for `net_cents` (post-fee net).

        Returns receipt FIELDS (the caller signs them): on success
        {"refused": False, "status": "ok", "mode", "hops": [...], ...};
        on refusal {"refused": True, "status": "refused_*", ...} with
        hops == [] (atomic: nothing executed on any refusal path).
        """
        if not path:
            return self._refusal("refused_no_path", "empty path", net_cents)
        if net_cents <= 0:
            return self._refusal("refused_dust_eaten_by_fees",
                                 "net_cents=%d <= 0" % net_cents, net_cents)

        # 1. Classify every hop FIRST — refuse before executing anything.
        hops: list[tuple[str, dict, dict]] = []
        for prev, cur in zip(path, path[1:]):
            kind = _classify_hop(prev, cur)
            hops.append((kind, prev, cur))
        if len(path) == 1:
            # Trivial origin-only path: single transfer of the node itself.
            hops.append(("transfer", path[0], path[0]))

        for kind, prev, cur in hops:
            if kind != "transfer":
                return self._refusal(
                    "refused_unwired_hop",
                    "hop %s %s/%s -> %s/%s needs a %s protocol; "
                    "no dex/bridge wired in v1 — refusing before any "
                    "execution (atomic)" % (
                        kind, prev["chain"], prev["token"],
                        cur["chain"], cur["token"], kind),
                    net_cents)

        # 2. Resolve adapters + per-hop amounts (still executing nothing).
        plan = []
        for _kind, _prev, cur in hops:
            chain, token = cur["chain"], cur["token"]
            adapter = self.adapters.get(chain)
            if adapter is None:
                return self._refusal(
                    "refused_unwired_hop",
                    "no chain adapter for %r" % chain, net_cents)
            ok, reason = adapter.can_transfer(token)
            if not ok:
                return self._refusal("refused_unwired_hop", reason, net_cents)
            price = oracle.get_price_usd(chain, token)
            if price is None:
                return self._refusal("refused_no_price",
                                     "%s on %s has no verifiable price"
                                     % (token, chain), net_cents)
            base_units = cents_to_base_units(
                net_cents, adapter.token_decimals(token), price)
            if base_units <= 0:
                return self._refusal(
                    "refused_dust_eaten_by_fees",
                    "net %d¢ of %s @ $%s = %d base units (dust)"
                    % (net_cents, token, price, base_units), net_cents)
            plan.append((adapter, chain, token, base_units, price))

        # 3. Recipient. Broadcast NEVER uses a throwaway recipient.
        first_chain = plan[0][1]
        first_family = self.adapters[first_chain].cfg["family"]
        throwaway_recipient = False
        recipient = to_address
        if self.mode == "broadcast":
            recipient = (recipient
                         or os.environ.get("AWL_SETTLER_RECIPIENT", "").strip()
                         or None)
            if not recipient:
                return self._refusal(
                    "refused_no_recipient",
                    "broadcast mode needs an explicit to_address (quote "
                    "field or AWL_SETTLER_RECIPIENT); refusing rather "
                    "than sending testnet funds to a throwaway",
                    net_cents)
            # Broadcast gate per chain, BEFORE any execution (fail closed).
            for adapter, chain, _t, _u, _p in plan:
                ok, reason = broadcast_allowed(chain)
                if not ok:
                    raise RuntimeError(reason)
        if not recipient:
            recipient = _throwaway_recipient(first_chain, first_family)
            throwaway_recipient = True
        recipient = _validate_recipient(first_chain, recipient, first_family)

        # 4. Execute. A broadcast failure raises LOUDLY (funds may be in
        # flight — the caller must surface that, not a quiet receipt).
        hop_results = []
        key_sources = set()
        for adapter, chain, token, base_units, price in plan:
            seed, key_source = self.key_provider(chain)
            key_sources.add(key_source)
            sender = adapter.address(seed)
            if self.mode == "dryrun":
                hop = adapter.dryrun_transfer(seed, recipient, base_units,
                                              token)
            else:
                hop = adapter.broadcast_transfer(seed, recipient, base_units,
                                                 token)
            hop_results.append({
                "hop": "transfer",
                "chain": chain,
                "token": token,
                "price_usd": price,
                "amount_base_units": base_units,
                "sender": sender,
                "to": recipient,
                "key_source": key_source,
                **hop,
            })

        return {
            "refused": False,
            "status": "ok",
            "mode": self.mode,
            "hops": hop_results,
            "to_address": recipient,
            "throwaway_recipient": throwaway_recipient,
            "key_source": sorted(key_sources)[0] if len(key_sources) == 1
            else sorted(key_sources),
            "net_cents": net_cents,
        }
