"""awLPay chain execution: real transaction build / sign / simulate / broadcast.

=====================================================================
SAFETY LAW — READ BEFORE TOUCHING
---------------------------------------------------------------------
* TESTNETS ONLY. The NETWORKS table below contains testnet configs
  exclusively (Sepolia, Base Sepolia, Polygon Amoy, Arbitrum Sepolia,
  Solana devnet). Mainnet chain ids (1, 8453, 137, 42161) and Solana
  mainnet-beta are HARD-REFUSED by assert_testnet() — no env var can
  override that. A mainnet flip is a deliberate CODE change plus
  Corey's explicit per-charge approval, never a config flip.
* AWL_MAINNET_ENABLED=1 (see settlement.py) refuses LOUDLY before any
  of this code is reached.
* Broadcasts are additionally gated on AWL_BROADCAST=1. Without it,
  broadcast mode RAISES instead of silently downgrading to dry-run.
* Keys: AWL_SETTLER_KEY_<chain> (64 hex chars = 32-byte seed). Unset
  -> a THROWAWAY key is generated per process and REPORTED as such.
  NEVER Corey's keys, never mainnet keys — this module must never see
  them. NO real funds move: testnet coins and throwaway keys only.
=====================================================================

Two adapter families:
    EvmAdapter    — web3.py. Native transfers + ERC-20 USDC transfers.
                    simulate() = eth_call against the testnet RPC.
    SolanaAdapter — solders + stdlib JSON-RPC. Native SOL system
                    transfers. simulate() = simulateTransaction with
                    replaceRecentBlockhash. SPL/USDC transfers are NOT
                    wired in v1 (can_transfer("USDC") is False, loud).

All network I/O lives in simulate()/broadcast()/fill methods. build_*
and sign() are pure offline operations (no RPC needed), which is what
makes dry-run construction unit-testable without a network.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.request

# --------------------------------------------------------------------------
# Testnet-only network table. THE mainnet-flip surface: replacing this
# table (a code change) plus Corey's explicit approval is the ONLY
# sanctioned path to mainnet. Env vars may override RPC URLs, never
# chain ids / clusters.
# --------------------------------------------------------------------------
_NETWORKS: dict[str, dict] = {
    "ethereum": {
        "label": "sepolia", "family": "evm", "chain_id": 11155111,
        "rpc_env": "AWL_RPC_ethereum",
        "rpc_default": "https://ethereum-sepolia-rpc.publicnode.com",
        "native": "ETH", "native_decimals": 18,
        "usdc": "0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238",
        "explorer": "https://sepolia.etherscan.io",
    },
    "base": {
        "label": "base-sepolia", "family": "evm", "chain_id": 84532,
        "rpc_env": "AWL_RPC_base",
        "rpc_default": "https://sepolia.base.org",
        "native": "ETH", "native_decimals": 18,
        "usdc": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
        "explorer": "https://sepolia.basescan.org",
    },
    "polygon": {
        "label": "amoy", "family": "evm", "chain_id": 80002,
        "rpc_env": "AWL_RPC_polygon",
        "rpc_default": "https://rpc-amoy.polygon.technology",
        "native": "ETH", "native_decimals": 18,
        "usdc": "0x41E94Eb019C0762f9Bfcf9Fb1E58725BfB0e7582",
        "explorer": "https://amoy.polygonscan.com",
    },
    "arbitrum": {
        "label": "arbitrum-sepolia", "family": "evm", "chain_id": 421614,
        "rpc_env": "AWL_RPC_arbitrum",
        "rpc_default": "https://sepolia-rollup.arbitrum.io/rpc",
        "native": "ETH", "native_decimals": 18,
        "usdc": "0x75faf114eafb1BDbe2F0316DF893fd58CE46AA4d",
        "explorer": "https://sepolia.arbiscan.io",
    },
    "solana": {
        "label": "devnet", "family": "solana", "cluster": "devnet",
        "rpc_env": "AWL_RPC_solana",
        "rpc_default": "https://api.devnet.solana.com",
        "native": "SOL", "native_decimals": 9,
        "usdc_mint": "4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU",
        "explorer": "https://explorer.solana.com/?cluster=devnet",
    },
}

# USDC decimals are 6 on every ecosystem (Circle's rule).
USDC_DECIMALS = 6

# Mainnet ids that can NEVER execute here, whatever the table says.
_MAINNET_EVM_CHAIN_IDS = {1, 8453, 137, 42161}
_MAINNET_SOLANA_CLUSTERS = {"mainnet-beta"}

_RPC_TIMEOUT_S = 15


def network_config(chain: str) -> dict:
    """Return a COPY of the network config for `chain` (testnet-only).

    RPC URL and USDC contract are env-overridable (AWL_RPC_<chain>,
    AWL_USDC_<chain>); chain id / cluster are NOT."""
    cfg = _NETWORKS.get(chain)
    if cfg is None:
        raise ValueError("unknown chain %r (want one of %s)"
                         % (chain, sorted(_NETWORKS)))
    cfg = dict(cfg)
    cfg["rpc_url"] = os.environ.get(cfg["rpc_env"], "").strip() \
        or cfg["rpc_default"]
    if cfg["family"] == "evm":
        cfg["usdc"] = os.environ.get("AWL_USDC_" + chain, "").strip() \
            or cfg["usdc"]
    else:
        cfg["usdc_mint"] = os.environ.get("AWL_USDC_MINT_solana",
                                          "").strip() or cfg["usdc_mint"]
    return cfg


def assert_testnet(chain: str) -> dict:
    """Hard guard: `chain` must resolve to a testnet config. Returns it.

    Raises RuntimeError on unknown chains, on mainnet EVM chain ids,
    and on non-devnet Solana clusters. No env var bypasses this."""
    cfg = network_config(chain)
    if cfg["family"] == "evm":
        if cfg["chain_id"] in _MAINNET_EVM_CHAIN_IDS:
            raise RuntimeError(
                "REFUSED: chain %r resolves to mainnet chain id %d — "
                "mainnet execution is not authorized" % (chain, cfg["chain_id"]))
    else:
        if cfg["cluster"] in _MAINNET_SOLANA_CLUSTERS:
            raise RuntimeError(
                "REFUSED: solana cluster %r is mainnet — not authorized"
                % cfg["cluster"])
        if cfg["cluster"] != "devnet":
            raise RuntimeError(
                "REFUSED: solana cluster %r is not a known testnet"
                % cfg["cluster"])
    return cfg


def broadcast_allowed(chain: str) -> tuple[bool, str]:
    """Broadcast gate: testnet-only AND AWL_BROADCAST=1. (ok, reason)."""
    assert_testnet(chain)  # raises on mainnet — never gated, always refused
    if os.environ.get("AWL_BROADCAST", "0") != "1":
        return False, ("refused: AWL_BROADCAST != 1 — broadcast gate closed; "
                       "not broadcasting")
    return True, "ok"


# --------------------------------------------------------------------------
# Settler keys: env seed or throwaway. 64 hex chars = 32-byte seed, same
# convention as AWL_RELAYER_KEY. NEVER Corey's keys.
# --------------------------------------------------------------------------
def get_settler_seed(chain: str) -> tuple[bytes, str]:
    """(seed_bytes, source) with source 'env' or 'throwaway'."""
    raw = os.environ.get("AWL_SETTLER_KEY_" + chain, "").strip()
    if raw:
        try:
            seed = bytes.fromhex(raw)
        except ValueError:
            raise SystemExit("AWL_SETTLER_KEY_%s must be 64 hex chars" % chain)
        if len(seed) != 32:
            raise SystemExit("AWL_SETTLER_KEY_%s must be 64 hex chars "
                             "(32-byte seed)" % chain)
        return seed, "env"
    return os.urandom(32), "throwaway"


# --------------------------------------------------------------------------
# EVM adapter (web3.py). Lazy import so `import server.chains` never
# requires web3 unless an EVM adapter actually runs.
# --------------------------------------------------------------------------
_ERC20_TRANSFER_SELECTOR = "a9059cbb"  # transfer(address,uint256)


def erc20_transfer_calldata(to_address: str, amount_base_units: int) -> str:
    """0x-prefixed calldata for transfer(to, amount)."""
    to = to_address.lower().replace("0x", "")
    return ("0x" + _ERC20_TRANSFER_SELECTOR
            + to.rjust(64, "0")
            + ("%x" % amount_base_units).rjust(64, "0"))


class EvmAdapter:
    """Native + ERC-20 USDC transfers on one EVM testnet."""

    def __init__(self, chain: str):
        self.chain = chain
        self.cfg = assert_testnet(chain)
        self._w3 = None

    # -- offline ---------------------------------------------------------
    def address(self, seed: bytes) -> str:
        from eth_account import Account
        return Account.from_key(seed).address

    def can_transfer(self, token: str) -> tuple[bool, str]:
        if token == self.cfg["native"] or token == "USDC":
            return True, "ok"
        return False, "evm adapter: token %r not wired" % token

    def token_decimals(self, token: str) -> int:
        if token == "USDC":
            return USDC_DECIMALS
        if token == self.cfg["native"]:
            return self.cfg["native_decimals"]
        raise ValueError("token %r not wired on %s" % (token, self.chain))

    def build_transfer(self, seed: bytes, to_address: str,
                       base_units: int, token: str,
                       nonce: int = 0) -> dict:
        """Unsigned legacy tx dict. Offline — needs no RPC.

        nonce defaults to 0 for offline signing; use fill_nonce() with
        a live RPC before broadcast."""
        ok, reason = self.can_transfer(token)
        if not ok:
            raise ValueError(reason)
        if base_units < 0:
            raise ValueError("base_units must be >= 0")
        from eth_utils import to_checksum_address
        to_address = to_checksum_address(to_address)  # validates + normalizes
        sender = self.address(seed)
        tx: dict = {
            "chainId": self.cfg["chain_id"],
            "nonce": nonce,
            "gasPrice": 1_000_000_000,  # 1 gwei placeholder; fill_live() refreshes
            "gas": 21000,
            "from": sender,
            "value": 0,
            "data": "0x",
        }
        if token == "USDC":
            tx["to"] = self.cfg["usdc"]
            tx["data"] = erc20_transfer_calldata(to_address, base_units)
            tx["gas"] = 65000
        else:
            tx["to"] = to_address
            tx["value"] = base_units
        return tx

    def sign(self, seed: bytes, tx: dict) -> dict:
        """Sign offline. Returns {raw_tx, tx_hash, sender}."""
        from eth_account import Account
        acct = Account.from_key(seed)
        signed = acct.sign_transaction(tx)
        return {
            "raw_tx": signed.raw_transaction.hex(),
            "tx_hash": signed.hash.hex(),
            "sender": acct.address,
        }

    # -- live ------------------------------------------------------------
    def _w3client(self):
        if self._w3 is None:
            from web3 import Web3
            self._w3 = Web3(Web3.HTTPProvider(
                self.cfg["rpc_url"],
                request_kwargs={"timeout": _RPC_TIMEOUT_S}))
        return self._w3

    def _check_chain_id(self) -> None:
        """The RPC must report the configured testnet chain id."""
        got = self._w3client().eth.chain_id
        want = self.cfg["chain_id"]
        if got != want:
            raise RuntimeError(
                "RPC chain id mismatch on %s: node=%d config=%d — refusing"
                % (self.chain, got, want))

    def fill_live(self, seed: bytes, tx: dict) -> dict:
        """Fill nonce + live gas price from the RPC (call before broadcast)."""
        w3 = self._w3client()
        self._check_chain_id()
        tx = dict(tx)
        tx["nonce"] = w3.eth.get_transaction_count(self.address(seed))
        tx["gasPrice"] = w3.eth.gas_price
        return tx

    def simulate(self, tx: dict) -> dict:
        """Dry-run via eth_call. Returns {ok, returned} or {ok, error}."""
        try:
            w3 = self._w3client()
            self._check_chain_id()
            call_tx = {k: tx[k] for k in
                       ("from", "to", "value", "data", "gas", "gasPrice")
                       if k in tx}
            returned = w3.eth.call(call_tx)
            return {"ok": True, "returned": returned.hex()}
        except Exception as e:  # noqa: BLE001 — node errors are data here
            return {"ok": False, "error": "%s: %s"
                    % (type(e).__name__, str(e)[:300])}

    def broadcast(self, raw_tx_hex: str) -> str:
        """Broadcast a signed raw tx. GATED: AWL_BROADCAST=1 + testnet."""
        ok, reason = broadcast_allowed(self.chain)
        if not ok:
            raise RuntimeError(reason)
        w3 = self._w3client()
        self._check_chain_id()
        raw = raw_tx_hex[2:] if raw_tx_hex.startswith("0x") else raw_tx_hex
        tx_hash = w3.eth.send_raw_transaction(bytes.fromhex(raw))
        return tx_hash.hex()

    # -- executor-facing convenience ------------------------------------
    def dryrun_transfer(self, seed: bytes, to_address: str,
                        base_units: int, token: str) -> dict:
        """build + sign + eth_call. Never broadcasts."""
        tx = self.build_transfer(seed, to_address, base_units, token)
        signed = self.sign(seed, tx)
        sim = self.simulate(tx)
        return {
            "tx_id": signed["tx_hash"],
            "raw_tx": "0x" + signed["raw_tx"],
            "sim_ok": sim["ok"],
            "sim_detail": sim.get("returned") or sim.get("error"),
            "broadcast": False,
        }

    def broadcast_transfer(self, seed: bytes, to_address: str,
                           base_units: int, token: str) -> dict:
        """build + fill_live + sign + broadcast. GATED (see broadcast())."""
        tx = self.fill_live(seed, self.build_transfer(
            seed, to_address, base_units, token))
        signed = self.sign(seed, tx)
        tx_hash = self.broadcast(signed["raw_tx"])
        return {
            "tx_id": tx_hash,
            "raw_tx": "0x" + signed["raw_tx"],
            "broadcast": True,
            "explorer": "%s/tx/%s" % (self.cfg["explorer"], tx_hash),
        }


# --------------------------------------------------------------------------
# Solana adapter (solders + stdlib JSON-RPC). Native SOL system transfers
# only in v1 — SPL/USDC is a declared stub (can_transfer False, loud).
# --------------------------------------------------------------------------
def _sol_rpc(url: str, method: str, params: list) -> dict:
    body = json.dumps({"jsonrpc": "2.0", "id": 1,
                       "method": method, "params": params}).encode()
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=_RPC_TIMEOUT_S) as r:
        return json.load(r)


class SolanaAdapter:
    """Native SOL system transfers on Solana devnet."""

    def __init__(self, chain: str = "solana"):
        if chain != "solana":
            raise ValueError("SolanaAdapter only serves 'solana', got %r"
                             % chain)
        self.chain = chain
        self.cfg = assert_testnet(chain)

    @property
    def rpc_url(self) -> str:
        return self.cfg["rpc_url"]

    # -- offline ---------------------------------------------------------
    def address(self, seed: bytes) -> str:
        from solders.keypair import Keypair
        return str(Keypair.from_seed(seed).pubkey())

    def can_transfer(self, token: str) -> tuple[bool, str]:
        if token == self.cfg["native"]:
            return True, "ok"
        return False, ("solana_spl_not_wired: SPL token %r transfers are not "
                       "wired in v1 (native SOL only)" % token)

    def token_decimals(self, token: str) -> int:
        if token == self.cfg["native"]:
            return self.cfg["native_decimals"]
        raise ValueError("solana_spl_not_wired: %r" % token)

    def build_transfer(self, seed: bytes, to_address: str,
                       lamports: int, blockhash=None) -> dict:
        """Build an unsigned system-transfer message. Offline.

        blockhash defaults to the zero hash (placeholder) — the node
        replaces it when simulating with replaceRecentBlockhash, and
        broadcast() always fetches a fresh one."""
        ok, reason = self.can_transfer(self.cfg["native"])
        if not ok:  # pragma: no cover — native always wired
            raise ValueError(reason)
        if lamports < 0:
            raise ValueError("lamports must be >= 0")
        from solders.hash import Hash
        from solders.keypair import Keypair
        from solders.message import Message
        from solders.pubkey import Pubkey
        from solders.system_program import TransferParams, transfer
        kp = Keypair.from_seed(seed)
        ix = transfer(TransferParams(
            from_pubkey=kp.pubkey(),
            to_pubkey=Pubkey.from_string(to_address),
            lamports=lamports))
        msg = Message.new_with_blockhash(
            [ix], kp.pubkey(), blockhash or Hash.default())
        return {"message": msg, "lamports": lamports, "to": to_address}

    def sign(self, seed: bytes, built: dict) -> dict:
        """Sign offline. Returns {tx_base64, signature, message_bytes}."""
        from solders.keypair import Keypair
        from solders.transaction import Transaction
        kp = Keypair.from_seed(seed)
        msg = built["message"]
        tx = Transaction.new_unsigned(msg)
        tx.sign([kp], msg.recent_blockhash)
        sig = tx.signatures[0]
        return {
            "tx_base64": base64.b64encode(bytes(tx)).decode(),
            "signature": str(sig),
            "sender": str(kp.pubkey()),
        }

    # -- live ------------------------------------------------------------
    def simulate(self, tx_base64: str) -> dict:
        """Dry-run via simulateTransaction (unsigned tx accepted: the
        node replaces the blockhash; sigVerify is off)."""
        try:
            resp = _sol_rpc(self.rpc_url, "simulateTransaction", [
                tx_base64,
                {"encoding": "base64", "sigVerify": False,
                 "replaceRecentBlockhash": True,
                 "commitment": "processed"},
            ])
            value = (resp.get("result") or {}).get("value") or {}
            err = value.get("err")
            return {"ok": err is None, "err": err,
                    "units_consumed": value.get("unitsConsumed"),
                    "slot": (resp.get("result") or {}).get("context", {})
                            .get("slot")}
        except Exception as e:  # noqa: BLE001 — node errors are data here
            return {"ok": False, "error": "%s: %s"
                    % (type(e).__name__, str(e)[:300])}

    def broadcast(self, seed: bytes, to_address: str, lamports: int) -> str:
        """Build with a FRESH blockhash, sign, sendTransaction. GATED."""
        ok, reason = broadcast_allowed(self.chain)
        if not ok:
            raise RuntimeError(reason)
        from solders.hash import Hash
        latest = _sol_rpc(self.rpc_url, "getLatestBlockhash",
                          [{"commitment": "confirmed"}])
        bh = Hash.from_string(
            latest["result"]["value"]["blockhash"])
        built = self.build_transfer(seed, to_address, lamports, blockhash=bh)
        signed = self.sign(seed, built)
        resp = _sol_rpc(self.rpc_url, "sendTransaction", [
            signed["tx_base64"],
            {"encoding": "base64", "skipPreflight": False,
             "preflightCommitment": "confirmed"},
        ])
        return resp["result"]

    # -- executor-facing convenience ------------------------------------
    def dryrun_transfer(self, seed: bytes, to_address: str,
                        lamports: int, token: str) -> dict:
        """build + sign + simulateTransaction. Never broadcasts."""
        ok, reason = self.can_transfer(token)
        if not ok:
            raise ValueError(reason)
        built = self.build_transfer(seed, to_address, lamports)
        signed = self.sign(seed, built)
        sim = self.simulate(signed["tx_base64"])
        return {
            "tx_id": signed["signature"],
            "signature": signed["signature"],
            "tx_base64": signed["tx_base64"],
            "sim_ok": sim["ok"],
            "sim_detail": sim.get("err") or sim.get("error"),
            "sim_units_consumed": sim.get("units_consumed"),
            "broadcast": False,
        }

    def broadcast_transfer(self, seed: bytes, to_address: str,
                           lamports: int, token: str) -> dict:
        """Fresh blockhash + sign + sendTransaction. GATED (see broadcast())."""
        ok, reason = self.can_transfer(token)
        if not ok:
            raise ValueError(reason)
        signature = self.broadcast(seed, to_address, lamports)
        return {
            "tx_id": signature,
            "signature": signature,
            "broadcast": True,
            "explorer": ("https://explorer.solana.com/tx/%s?cluster=devnet"
                         % signature),
        }
