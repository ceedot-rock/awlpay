"""Rail adapters for the awlpay SDK.

A rail is a chain + stablecoin pair the wallet can pay on. v1 ships
Base (USDC) and Solana (USDC) — the same two chains Cloudflare covers —
behind a pluggable registry so more rails slot in later without touching
the wallet logic.

Testnet is the default network. Mainnet configs exist but the wallet
only uses them after an explicit typed confirmation.
"""

from __future__ import annotations

import json
import struct
import time
import urllib.request
from dataclasses import dataclass

USDC_DECIMALS = 6

# Rough fee estimates in USD, used ONLY to order rails for "auto".
# Not a quote — real fees come from the chain at send time.
FEE_ESTIMATE_USD = {"solana": 0.0003, "base": 0.02}

# Public testnet RPCs are flaky; retry transient failures once.
_RPC_RETRIES = 2


def _with_retry(fn, desc: str):
    """Run ``fn``; retry transient network failures, then raise RailError."""
    from .errors import RailError

    last: Exception | None = None
    for _ in range(_RPC_RETRIES):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 — wrapped below
            last = exc
            time.sleep(1)
    raise RailError(
        f"{desc} failed after {_RPC_RETRIES} attempts ({type(last).__name__}: "
        f"{last}). The public testnet RPC is likely flaky — retry, or point "
        f"the rail at your own RPC endpoint."
    ) from last

RAILS: dict[str, dict] = {
    "base": {
        "label": "Base",
        "family": "evm",
        "testnet": {
            "chain_id": 84532,
            "rpc": "https://sepolia.base.org",
            "usdc": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
            "explorer": "https://sepolia.basescan.org",
        },
        "mainnet": {
            "chain_id": 8453,
            "rpc": "https://mainnet.base.org",
            "usdc": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "explorer": "https://basescan.org",
        },
    },
    "solana": {
        "label": "Solana",
        "family": "solana",
        "testnet": {
            "cluster": "devnet",
            "rpc": "https://api.devnet.solana.com",
            "usdc_mint": "4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU",
            "explorer": "https://explorer.solana.com/?cluster=devnet",
        },
        "mainnet": {
            "cluster": "mainnet-beta",
            "rpc": "https://api.mainnet-beta.solana.com",
            "usdc_mint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            "explorer": "https://explorer.solana.com",
        },
    },
}

# Minimal ERC-20 ABI: balanceOf + transfer.
_ERC20_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "type": "function",
    },
    {
        "constant": False,
        "inputs": [
            {"name": "_to", "type": "address"},
            {"name": "_value", "type": "uint256"},
        ],
        "name": "transfer",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function",
    },
]


@dataclass
class RailAdapter:
    """Base class. Subclasses implement one chain family."""

    name: str
    network: str  # "testnet" | "mainnet"

    @property
    def config(self) -> dict:
        return RAILS[self.name][self.network]

    @property
    def fee_estimate_usd(self) -> float:
        return FEE_ESTIMATE_USD[self.name]

    def new_keypair(self) -> dict:
        """Generate a fresh keypair. Returns {"private_key": hex, "address": str}."""
        raise NotImplementedError

    def address_from_key(self, private_key_hex: str) -> str:
        raise NotImplementedError

    def balance_usd(self, address: str) -> float:
        """USDC balance converted to USD (1 USDC == 1 USD by definition)."""
        raise NotImplementedError

    def pay(self, private_key_hex: str, to_address: str, usd: float) -> str:
        """Build, sign, submit, wait for confirmation. Returns the tx hash."""
        raise NotImplementedError


# --------------------------------------------------------------------------
# Base (EVM / USDC)
# --------------------------------------------------------------------------


class BaseRail(RailAdapter):
    def __init__(self, network: str = "testnet"):
        super().__init__("base", network)

    def _w3(self):
        from web3 import Web3

        return Web3(Web3.HTTPProvider(self.config["rpc"], request_kwargs={"timeout": 30}))

    def _contract(self, w3):
        return w3.eth.contract(
            address=w3.to_checksum_address(self.config["usdc"]), abi=_ERC20_ABI
        )

    def new_keypair(self) -> dict:
        from eth_account import Account

        acct = Account.create()
        return {"private_key": acct.key.hex(), "address": acct.address}

    def address_from_key(self, private_key_hex: str) -> str:
        from eth_account import Account

        return Account.from_key(private_key_hex).address

    def balance_usd(self, address: str) -> float:
        def _call():
            w3 = self._w3()
            contract = self._contract(w3)
            raw = contract.functions.balanceOf(
                w3.to_checksum_address(address)
            ).call()
            return raw / 10**USDC_DECIMALS

        return _with_retry(_call, "Base USDC balance")

    def pay(self, private_key_hex: str, to_address: str, usd: float) -> str:
        from eth_account import Account

        w3 = self._w3()
        contract = self._contract(w3)
        sender = Account.from_key(private_key_hex)
        amount = int(round(usd * 10**USDC_DECIMALS))
        if amount <= 0:
            raise ValueError("usd must be positive")
        tx = contract.functions.transfer(
            w3.to_checksum_address(to_address), amount
        ).build_transaction(
            {
                "chainId": self.config["chain_id"],
                "from": sender.address,
                "nonce": w3.eth.get_transaction_count(sender.address),
                "gas": 100_000,
                "gasPrice": w3.eth.gas_price,
            }
        )
        signed = sender.sign_transaction(tx)

        def _submit_and_wait():
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
            if receipt.status != 1:
                raise RuntimeError(f"Base transfer reverted: {tx_hash.hex()}")
            return tx_hash.hex()

        return _with_retry(_submit_and_wait, "Base transfer submit")


# --------------------------------------------------------------------------
# Solana (SPL USDC) — solders + stdlib JSON-RPC, no solana-py needed.
# --------------------------------------------------------------------------

_TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
_ASSOC_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efYaRABWgw"
_SYSTEM_PROGRAM = "11111111111111111111111111111111"


def _sol_rpc(rpc_url: str, method: str, params: list):
    def _call():
        body = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        ).encode()
        req = urllib.request.Request(
            rpc_url, data=body, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            out = json.loads(resp.read().decode())
        if out.get("error"):
            raise RuntimeError(f"Solana RPC error: {out['error']}")
        return out["result"]

    return _with_retry(_call, f"Solana RPC {method}")


class SolanaRail(RailAdapter):
    def __init__(self, network: str = "testnet"):
        super().__init__("solana", network)

    def _ata(self, owner, mint):
        from solders.pubkey import Pubkey

        token_program = Pubkey.from_string(_TOKEN_PROGRAM)
        assoc_program = Pubkey.from_string(_ASSOC_PROGRAM)
        seeds = [bytes(owner), bytes(token_program), bytes(mint)]
        addr, _ = Pubkey.find_program_address(seeds, assoc_program)
        return addr

    def new_keypair(self) -> dict:
        from solders.keypair import Keypair

        kp = Keypair()
        return {"private_key": kp.secret().hex(), "address": str(kp.pubkey())}

    def address_from_key(self, private_key_hex: str) -> str:
        from solders.keypair import Keypair

        return str(Keypair.from_bytes(bytes.fromhex(private_key_hex)).pubkey())

    def _token_accounts(self, owner_str: str) -> list:
        from solders.pubkey import Pubkey

        owner = Pubkey.from_string(owner_str)
        mint = Pubkey.from_string(self.config["usdc_mint"])
        res = _sol_rpc(
            self.config["rpc"],
            "getTokenAccountsByOwner",
            [str(owner), {"mint": str(mint)}, {"encoding": "jsonParsed"}],
        )
        return res["value"]

    def balance_usd(self, address: str) -> float:
        total = 0
        for acct in self._token_accounts(address):
            info = acct["account"]["data"]["parsed"]["info"]
            total += int(info["tokenAmount"]["amount"])
        return total / 10**USDC_DECIMALS

    def pay(self, private_key_hex: str, to_address: str, usd: float) -> str:
        from solders.hash import Hash
        from solders.instruction import AccountMeta, Instruction
        from solders.keypair import Keypair
        from solders.message import Message
        from solders.pubkey import Pubkey
        from solders.transaction import Transaction

        import base64

        amount = int(round(usd * 10**USDC_DECIMALS))
        if amount <= 0:
            raise ValueError("usd must be positive")

        kp = Keypair.from_bytes(bytes.fromhex(private_key_hex))
        owner = kp.pubkey()
        mint = Pubkey.from_string(self.config["usdc_mint"])
        token_program = Pubkey.from_string(_TOKEN_PROGRAM)
        assoc_program = Pubkey.from_string(_ASSOC_PROGRAM)
        system_program = Pubkey.from_string(_SYSTEM_PROGRAM)
        dest_owner = Pubkey.from_string(to_address)

        src_ata = self._ata(owner, mint)
        dst_ata = self._ata(dest_owner, mint)

        ixs: list[Instruction] = []

        # Create the destination ATA if it doesn't exist yet.
        exists = _sol_rpc(
            self.config["rpc"],
            "getAccountInfo",
            [str(dst_ata), {"encoding": "base64"}],
        )["value"]
        if exists is None:
            ixs.append(
                Instruction(
                    program_id=assoc_program,
                    data=bytes([1]),  # create
                    accounts=[
                        AccountMeta(pubkey=owner, is_signer=True, is_writable=True),
                        AccountMeta(pubkey=dst_ata, is_signer=False, is_writable=True),
                        AccountMeta(pubkey=dest_owner, is_signer=False, is_writable=False),
                        AccountMeta(pubkey=mint, is_signer=False, is_writable=False),
                        AccountMeta(pubkey=system_program, is_signer=False, is_writable=False),
                        AccountMeta(pubkey=token_program, is_signer=False, is_writable=False),
                    ],
                )
            )

        # SPL Token transfer: discriminator 3 + u64 LE amount.
        ixs.append(
            Instruction(
                program_id=token_program,
                data=bytes([3]) + struct.pack("<Q", amount),
                accounts=[
                    AccountMeta(pubkey=src_ata, is_signer=False, is_writable=True),
                    AccountMeta(pubkey=dst_ata, is_signer=False, is_writable=True),
                    AccountMeta(pubkey=owner, is_signer=True, is_writable=False),
                ],
            )
        )

        blockhash = Hash.from_string(
            _sol_rpc(self.config["rpc"], "getLatestBlockhash", [])["value"]["blockhash"]
        )
        tx = Transaction.new_signed_with_payer(ixs, owner, [kp], blockhash)
        raw_b64 = base64.b64encode(bytes(tx)).decode()
        sig = _sol_rpc(
            self.config["rpc"],
            "sendTransaction",
            [raw_b64, {"encoding": "base64", "preflightCommitment": "confirmed"}],
        )

        deadline = time.time() + 120
        while time.time() < deadline:
            statuses = _sol_rpc(
                self.config["rpc"], "getSignatureStatuses", [[sig]]
            )["value"]
            st = statuses[0] if statuses else None
            if st and st.get("err"):
                raise RuntimeError(f"Solana transfer failed: {st['err']}")
            if st and st.get("confirmationStatus") in ("confirmed", "finalized"):
                return sig
            time.sleep(2)
        raise TimeoutError(f"Solana transfer not confirmed in time: {sig}")


def make_rail(name: str, network: str) -> RailAdapter:
    if name == "base":
        return BaseRail(network)
    if name == "solana":
        return SolanaRail(network)
    from .errors import UnsupportedRail

    raise UnsupportedRail(f"Unknown rail {name!r}. Supported: base, solana")
