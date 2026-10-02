"""awLPay ConverterRouter: anything-to-anything conversion.

Law: a conversion path is legal only if EVERY token on it hasValue()
(oracle price not None). If no path exists, or any hop's token is
unpriced, the router returns None and the caller MUST refuse the quote
with a reason — never route through unpriced tokens.

The graph is DATA, not code: chains/tokens/edges live in module-level
tables below so real liquidity sources (bridge registries, DEX quotes)
can plug in later without touching the pathfinding.

Nodes are (chain, token) pairs. Edges carry a "hop" type:
    "bridge" — same token across chains (cross-chain bridge)
    "ccip"   — same token across chains via the Chainlink CCIP 2.0 rail
    "swap"   — different tokens on the same chain (DEX-style swap)
Pathfinding is plain BFS over the legal subgraph (all tokens priced).
"""

from __future__ import annotations

from collections import deque

from .oracle import PriceOracle

# --------------------------------------------------------------------------
# v1 static graph (chains/tokens from the locked product spec)
# --------------------------------------------------------------------------
CHAINS = ["ethereum", "base", "polygon", "arbitrum", "solana"]
TOKENS = ["USDC", "ETH", "SOL"]

# Same-token cross-chain bridge hops: (chain_a, chain_b) pairs for USDC.
# Bridges move the token, not the value — both ends are USDC, so no
# swap leg is needed.
USDC_BRIDGES = [
    ("ethereum", "base"),
    ("ethereum", "polygon"),
    ("ethereum", "arbitrum"),
    ("base", "polygon"),
    ("base", "arbitrum"),
    ("polygon", "arbitrum"),
    ("ethereum", "solana"),
]

# Chainlink CCIP 2.0 rail (added 2026-09-29, Corey's call): same-token
# cross-chain hops for pairs the generic bridge table doesn't cover.
# CCIP 2.0 keeps the 16-operator committee by default and lets issuers
# add a custom Cross-Chain Verifier to co-sign select transfers, with
# configurable speeds and built-in KYC/AML/sanctions screening — the
# closest plug-in candidate for this router's cross-chain path.
CCIP_BRIDGES = [
    ("base", "solana"),
    ("polygon", "solana"),
    ("arbitrum", "solana"),
]

# Same-chain swap hops: (chain, token_a, token_b). ETH<->USDC on every EVM
# chain; SOL<->USDC on Solana. (ETH only lives on EVM chains here; SOL
# only on Solana — no bridge edges for them in v1.)
SWAPS = [
    ("ethereum", "ETH", "USDC"),
    ("base", "ETH", "USDC"),
    ("polygon", "ETH", "USDC"),
    ("arbitrum", "ETH", "USDC"),
    ("solana", "SOL", "USDC"),
]


def _build_graph() -> dict[tuple[str, str], list[dict]]:
    """adjacency: node -> [{chain, token, hop}, ...]"""
    graph: dict[tuple[str, str], list[dict]] = {}

    def node(chain: str, token: str) -> tuple[str, str]:
        return (chain, token)

    def add_edge(a: tuple[str, str], b: tuple[str, str], hop: str) -> None:
        graph.setdefault(a, []).append({"chain": b[0], "token": b[1], "hop": hop})

    # Nodes: every chain x every token that plausibly exists there.
    supported = {
        "ethereum": ["USDC", "ETH"],
        "base": ["USDC", "ETH"],
        "polygon": ["USDC", "ETH"],
        "arbitrum": ["USDC", "ETH"],
        "solana": ["USDC", "SOL"],
    }
    for chain, tokens in supported.items():
        for token in tokens:
            graph.setdefault(node(chain, token), [])

    for a, b in USDC_BRIDGES:
        if node(a, "USDC") in graph and node(b, "USDC") in graph:
            add_edge(node(a, "USDC"), node(b, "USDC"), "bridge")
            add_edge(node(b, "USDC"), node(a, "USDC"), "bridge")

    for a, b in CCIP_BRIDGES:
        if node(a, "USDC") in graph and node(b, "USDC") in graph:
            add_edge(node(a, "USDC"), node(b, "USDC"), "ccip")
            add_edge(node(b, "USDC"), node(a, "USDC"), "ccip")

    for chain, ta, tb in SWAPS:
        na, nb = node(chain, ta), node(chain, tb)
        if na in graph and nb in graph:
            add_edge(na, nb, "swap")
            add_edge(nb, na, "swap")

    return graph


GRAPH = _build_graph()


class ConverterRouter:
    """Finds a priced conversion path, or None (caller refuses)."""

    def __init__(self, graph: dict | None = None):
        self.graph = graph if graph is not None else GRAPH

    def find_path(self, from_chain: str, from_token: str,
                  to_chain: str, to_token: str,
                  oracle: PriceOracle) -> list[dict] | None:
        """Return the path as a list of steps:
            [{"chain", "token", "hop": "origin"|"bridge"|"swap"}, ...]
        or None when no legal path exists. A path is legal only if every
        token on it hasValue() per the oracle."""
        src = (str(from_chain).lower(), str(from_token).upper())
        dst = (str(to_chain).lower(), str(to_token).upper())

        if src not in self.graph or dst not in self.graph:
            return None  # unknown chain or token: no path
        if src == dst:
            # Trivial path still needs hasValue() — same token must be priced.
            if not oracle.has_value(src[0], src[1]):
                return None
            return [{"chain": src[0], "token": src[1], "hop": "origin"}]

        # BFS over the subgraph of priced nodes only.
        def priced(n: tuple[str, str]) -> bool:
            return oracle.has_value(n[0], n[1])

        if not priced(src) or not priced(dst):
            return None

        prev: dict[tuple[str, str], tuple[tuple[str, str], str] | None] = {src: None}
        queue = deque([src])
        while queue:
            cur = queue.popleft()
            if cur == dst:
                break
            for edge in self.graph[cur]:
                nxt = (edge["chain"], edge["token"])
                if nxt in prev or not priced(nxt):
                    continue
                prev[nxt] = (cur, edge["hop"])
                queue.append(nxt)

        if dst not in prev:
            return None

        # Reconstruct: walk back from dst, then reverse.
        hops: list[tuple[tuple[str, str], str]] = []
        cur = dst
        while cur is not None:
            back = prev[cur]
            hop = "origin" if back is None else back[1]
            hops.append((cur, hop))
            cur = back[0] if back else None
        hops.reverse()
        return [{"chain": c, "token": t, "hop": h} for (c, t), h in hops]
