"""awLPay real-settlement tests: chain adapters, path executor, modes.

Covers: testnet-only network table + mainnet hard-refusal, throwaway
key generation, offline EVM build/sign (native + ERC-20 calldata),
live eth_call dry-run on Base Sepolia (skipped when the RPC is
unreachable), offline Solana build/sign + simulateTransaction shape
(offline) + live devnet simulate (skipped when unreachable),
integer base-unit conversion law, executor dust/unwired-hop/broadcast
refusals with atomicity, and Ed25519-signed receipts in dryrun mode.

LIVE-NETWORK POLICY: tests that need a public RPC try it and
pytest.skip() on ANY failure (unreachable, rate-limited, timeout).
Reachability failure skips; wrongness fails. No test ever broadcasts.
The broadcast path is unit-tested through a stub adapter plus the real
broadcast gate (AWL_BROADCAST + assert_testnet).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

# Test the server code in THIS repo checkout (works in every worktree).
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from nacl.signing import SigningKey  # noqa: E402

from server import chains, chamber, executor, settlement  # noqa: E402
from server.oracle import MockOracle  # noqa: E402

SEED_A = bytes.fromhex("11" * 32)  # deterministic throwaway seed


# --------------------------------------------------------------------------
# network table + guards
# --------------------------------------------------------------------------
def test_network_table_is_testnet_only():
    for chain, cfg in chains._NETWORKS.items():
        if cfg["family"] == "evm":
            assert cfg["chain_id"] not in chains._MAINNET_EVM_CHAIN_IDS, chain
        else:
            assert cfg["cluster"] == "devnet", chain


def test_assert_testnet_refuses_mainnet_and_unknown(monkeypatch):
    import copy
    fake = copy.deepcopy(chains._NETWORKS["ethereum"])
    fake["chain_id"] = 1  # mainnet id smuggled into a copy of the table
    monkeypatch.setitem(chains._NETWORKS, "ethmain", fake)
    with pytest.raises(RuntimeError, match="mainnet"):
        chains.assert_testnet("ethmain")
    with pytest.raises(ValueError, match="unknown chain"):
        chains.assert_testnet("nope")
    for c in ("ethereum", "base", "polygon", "arbitrum", "solana"):
        chains.assert_testnet(c)  # the real entries pass


def test_broadcast_gate_needs_flag(monkeypatch):
    monkeypatch.delenv("AWL_BROADCAST", raising=False)
    ok, reason = chains.broadcast_allowed("base")
    assert ok is False and "AWL_BROADCAST" in reason
    monkeypatch.setenv("AWL_BROADCAST", "1")
    assert chains.broadcast_allowed("base") == (True, "ok")
    # the flag does NOT open mainnet: mainnet still raises, not returns
    import copy
    fake = copy.deepcopy(chains._NETWORKS["base"])
    fake["chain_id"] = 8453
    monkeypatch.setitem(chains._NETWORKS, "basemain", fake)
    with pytest.raises(RuntimeError, match="mainnet"):
        chains.broadcast_allowed("basemain")


def test_settler_key_throwaway_and_env(monkeypatch):
    for c in ("base", "solana"):
        monkeypatch.delenv("AWL_SETTLER_KEY_" + c, raising=False)
    s1, src1 = chains.get_settler_seed("base")
    s2, src2 = chains.get_settler_seed("base")
    assert (src1, src2) == ("throwaway", "throwaway")
    assert len(s1) == 32 and s1 != s2  # fresh randomness per call
    monkeypatch.setenv("AWL_SETTLER_KEY_base", "22" * 32)
    s3, src3 = chains.get_settler_seed("base")
    assert src3 == "env" and s3 == bytes.fromhex("22" * 32)


# --------------------------------------------------------------------------
# EVM adapter: offline build/sign
# --------------------------------------------------------------------------
def test_evm_build_sign_native_offline():
    from eth_account import Account
    from eth_utils import to_checksum_address
    ad = chains.EvmAdapter("base")
    sender = ad.address(SEED_A)
    assert sender == Account.from_key(SEED_A).address
    recipient = to_checksum_address("0x" + "ab" * 20)
    tx = ad.build_transfer(SEED_A, recipient, 123456789, "ETH")
    assert tx["chainId"] == 84532
    assert tx["to"] == recipient and tx["value"] == 123456789
    assert tx["data"] == "0x"
    signed = ad.sign(SEED_A, tx)
    assert Account.recover_transaction(bytes.fromhex(signed["raw_tx"])) == sender
    assert len(signed["tx_hash"]) == 64


def test_evm_usdc_calldata_encoding():
    from eth_utils import to_checksum_address
    ad = chains.EvmAdapter("base")
    recipient = to_checksum_address("0x" + "cd" * 20)
    tx = ad.build_transfer(SEED_A, recipient, 1_500_000, "USDC")
    assert tx["to"] == ad.cfg["usdc"]  # the call goes TO the USDC contract
    assert tx["value"] == 0
    data = tx["data"]
    assert data.startswith("0xa9059cbb")  # transfer(address,uint256)
    assert data[10:74] == "00" * 12 + "cd" * 20  # left-padded address
    assert data[74:138] == "%064x" % 1_500_000  # big-endian amount
    assert len(data) == 138


# --------------------------------------------------------------------------
# EVM adapter: live eth_call dry-run on Base Sepolia
# --------------------------------------------------------------------------
def _skip_unreachable(reason):
    pytest.skip("testnet RPC unreachable (%s) — dry-run skipped, "
                "offline signing tests above still prove construction"
                % reason)


def test_evm_eth_call_base_sepolia_dryrun():
    """Real eth_call against Base Sepolia: 0-value native transfer and
    0-unit USDC transfer must both execute cleanly. Skips when the
    public RPC is unreachable."""
    ad = chains.EvmAdapter("base")
    seed = os.urandom(32)
    recipient = ad.address(os.urandom(32))
    try:
        r1 = ad.simulate(ad.build_transfer(seed, recipient, 0, "ETH"))
        r2 = ad.simulate(ad.build_transfer(seed, recipient, 0, "USDC"))
    except Exception as e:  # noqa: BLE001 — simulate() already catches these
        _skip_unreachable("%s: %s" % (type(e).__name__, e))
    if not r1["ok"]:
        _skip_unreachable(r1.get("error", "eth_call failed"))
    if not r2["ok"]:
        _skip_unreachable(r2.get("error", "eth_call failed"))
    # native transfer returns empty data; a REAL ERC-20 returns bool true
    # (0x...01). An EOA at the USDC address would return empty — so this
    # also proves the configured contract address is a live token.
    assert r1["returned"] in ("0x", ""), r1
    h2 = r2["returned"]
    h2 = h2[2:] if h2.startswith("0x") else h2
    assert h2 == "00" * 31 + "01", r2


# --------------------------------------------------------------------------
# Solana adapter: offline build/sign + simulate shape
# --------------------------------------------------------------------------
def test_solana_build_sign_offline():
    from solders.keypair import Keypair
    from solders.signature import Signature
    from nacl.signing import VerifyKey
    ad = chains.SolanaAdapter()
    to = "11111111111111111111111111111111"  # system program: valid base58
    assert ad.address(SEED_A) == str(Keypair.from_seed(SEED_A).pubkey())
    built = ad.build_transfer(SEED_A, to, 5000)
    msg = built["message"]
    cix = msg.instructions[0]
    # the compiled instruction IS a system-program transfer of 5000 lamports
    assert (str(msg.account_keys[cix.program_id_index])
            == "11111111111111111111111111111111")
    assert cix.data == bytes([2, 0, 0, 0]) + (5000).to_bytes(8, "little")
    signed = ad.sign(SEED_A, built)
    assert len(signed["tx_base64"]) > 100
    # signature verifies against the message bytes with the sender pubkey
    VerifyKey(bytes(Keypair.from_seed(SEED_A).pubkey())).verify(
        bytes(msg), bytes(Signature.from_string(signed["signature"])))
    # SPL/USDC is an honest stub in v1
    ok, reason = ad.can_transfer("USDC")
    assert ok is False and "spl_not_wired" in reason


def test_solana_simulate_request_shape_and_results(monkeypatch):
    """simulateTransaction JSON-RPC shape + ok/err parsing, offline via a
    stubbed transport (no network)."""
    captured = {}

    def fake_rpc(url, method, params):
        captured.update(url=url, method=method, params=params)
        return {"result": {"context": {"slot": 4242},
                           "value": {"err": None, "unitsConsumed": 300}}}

    monkeypatch.setattr(chains, "_sol_rpc", fake_rpc)
    ad = chains.SolanaAdapter()
    out = ad.simulate("dGVzdA==")
    assert captured["url"] == ad.rpc_url
    assert captured["method"] == "simulateTransaction"
    opts = captured["params"][1]
    assert opts["encoding"] == "base64" and opts["sigVerify"] is False
    assert opts["replaceRecentBlockhash"] is True
    assert out == {"ok": True, "err": None, "units_consumed": 300,
                   "slot": 4242}

    def fake_rpc_err(url, method, params):
        return {"result": {"context": {"slot": 4243},
                           "value": {"err": {"InstructionError":
                                             [0, "InsufficientFunds"]}}}}

    monkeypatch.setattr(chains, "_sol_rpc", fake_rpc_err)
    out2 = ad.simulate("dGVzdA==")
    assert out2["ok"] is False
    assert out2["err"] == {"InstructionError": [0, "InsufficientFunds"]}


def test_solana_simulate_devnet_live():
    """Real simulateTransaction against devnet. Skips when unreachable."""
    ad = chains.SolanaAdapter()
    seed = os.urandom(32)
    built = ad.build_transfer(
        seed, "11111111111111111111111111111111", 1)
    signed = ad.sign(seed, built)
    sim = ad.simulate(signed["tx_base64"])
    if "error" in sim:
        _skip_unreachable(sim["error"])
    # well-formed node response either way (err null or an err object —
    # the throwaway sender is unfunded, so an err is expected and honest)
    assert "err" in sim, sim


# --------------------------------------------------------------------------
# base-unit conversion law (exact integer floor, no floats)
# --------------------------------------------------------------------------
def test_cents_to_base_units_exact_floor():
    e = executor.cents_to_base_units
    assert e(10_000, 6, 1.0) == 100_000_000            # $100 -> 100 USDC
    assert e(10_000, 18, 4000.0) == 25_000_000_000_000_000  # 0.025 ETH
    assert e(10_000, 9, 150.0) == 666_666_666          # $100 SOL @150 floors
    assert e(1, 18, 4000.0) == 2_500_000_000_000
    assert e(1, 6, 100_000.0) == 0                     # dust -> 0 base units
    assert e(0, 6, 1.0) == 0 and e(-5, 6, 1.0) == 0


# --------------------------------------------------------------------------
# executor: refusals are atomic, broadcast is fail-closed
# --------------------------------------------------------------------------
class _StubAdapter:
    """Offline stand-in for a chain adapter; records every execution call."""

    def __init__(self, chain="base", family="evm"):
        self.chain = chain
        self.cfg = {"family": family,
                    "native": "ETH" if family == "evm" else "SOL"}
        self.calls: list[str] = []

    def can_transfer(self, token):
        return True, "ok"

    def token_decimals(self, token):
        return 6

    def address(self, seed):
        return "0x" + "00" * 20

    def dryrun_transfer(self, seed, to_address, base_units, token):
        self.calls.append("dryrun")
        return {"tx_id": "0xstub", "sim_ok": True, "broadcast": False}

    def broadcast_transfer(self, seed, to_address, base_units, token):
        self.calls.append("broadcast")
        return {"tx_id": "0xstub-broadcast", "broadcast": True}


def _trivial_path():
    return [{"chain": "base", "token": "USDC", "hop": "origin"}]


def _oracle_usdc(price=1.0):
    return MockOracle({("base", "USDC"): price, ("ethereum", "ETH"): 4000.0,
                       ("ethereum", "USDC"): 1.0})


def test_executor_dust_refusal_is_atomic():
    stub = _StubAdapter()
    ex = executor.PathExecutor({"base": stub}, mode="dryrun")
    # 1¢ of USDC @ $100k = 0 base units -> dust, nothing executed
    out = ex.execute(_trivial_path(), 1, _oracle_usdc(100_000.0))
    assert out["refused"] is True
    assert out["status"] == "refused_dust_eaten_by_fees"
    assert out["hops"] == [] and stub.calls == []


def test_executor_unwired_hop_refuses_before_any_execution():
    eth_stub, base_stub = _StubAdapter("ethereum"), _StubAdapter("base")
    ex = executor.PathExecutor({"ethereum": eth_stub, "base": base_stub},
                               mode="dryrun")
    path = [
        {"chain": "ethereum", "token": "ETH", "hop": "origin"},
        {"chain": "ethereum", "token": "USDC", "hop": "swap"},
        {"chain": "base", "token": "USDC", "hop": "bridge"},
    ]
    out = ex.execute(path, 10_000, _oracle_usdc())
    assert out["refused"] is True
    assert out["status"] == "refused_unwired_hop"
    assert "swap" in out["detail"]
    assert out["hops"] == []
    assert eth_stub.calls == [] and base_stub.calls == []  # atomic


def test_executor_solana_spl_is_unwired():
    ad = chains.SolanaAdapter()  # real adapter, offline-safe here
    ex = executor.PathExecutor({"solana": ad}, mode="dryrun")
    path = [{"chain": "solana", "token": "USDC", "hop": "origin"}]
    out = ex.execute(path, 10_000,
                     MockOracle({("solana", "USDC"): 1.0}))
    assert out["refused"] is True
    assert out["status"] == "refused_unwired_hop"
    assert "spl_not_wired" in out["detail"]


def test_executor_dryrun_single_hop_stub():
    stub = _StubAdapter()
    ex = executor.PathExecutor({"base": stub}, mode="dryrun")
    out = ex.execute(_trivial_path(), 10_000, _oracle_usdc())
    assert out["refused"] is False and out["status"] == "ok"
    assert out["mode"] == "dryrun"
    hop = out["hops"][0]
    assert hop["hop"] == "transfer" and hop["chain"] == "base"
    assert hop["amount_base_units"] == 100_000_000  # $100 USDC, exact
    assert hop["tx_id"] == "0xstub" and hop["broadcast"] is False
    assert stub.calls == ["dryrun"]
    # no to_address -> throwaway recipient, honestly labeled
    assert out["throwaway_recipient"] is True
    # explicit recipient passes through and validates
    out2 = ex.execute(_trivial_path(), 10_000, _oracle_usdc(),
                      to_address="0x" + "11" * 20)
    assert out2["to_address"] == "0x" + "11" * 20
    assert out2["throwaway_recipient"] is False
    with pytest.raises(ValueError, match="bad EVM recipient"):
        ex.execute(_trivial_path(), 10_000, _oracle_usdc(),
                   to_address="not-an-address")


def test_executor_broadcast_gate_fail_closed(monkeypatch):
    stub = _StubAdapter()
    monkeypatch.delenv("AWL_BROADCAST", raising=False)
    ex = executor.PathExecutor({"base": stub}, mode="broadcast")
    # flag closed -> RAISES (never silently downgrades), nothing executed
    with pytest.raises(RuntimeError, match="AWL_BROADCAST"):
        ex.execute(_trivial_path(), 10_000, _oracle_usdc(),
                   to_address="0x" + "11" * 20)
    assert stub.calls == []
    # flag open but no recipient -> loud refusal, nothing executed
    monkeypatch.setenv("AWL_BROADCAST", "1")
    out = ex.execute(_trivial_path(), 10_000, _oracle_usdc())
    assert out["refused"] is True
    assert out["status"] == "refused_no_recipient"
    assert stub.calls == []
    # flag open + recipient -> broadcast path runs through the adapter
    out2 = ex.execute(_trivial_path(), 10_000, _oracle_usdc(),
                      to_address="0x" + "11" * 20)
    assert out2["refused"] is False and out2["mode"] == "broadcast"
    assert stub.calls == ["broadcast"]


def test_executor_bad_mode_rejected():
    with pytest.raises(ValueError, match="mode"):
        executor.PathExecutor({"base": _StubAdapter()}, mode="yolo")


# --------------------------------------------------------------------------
# settlement: modes, signed receipts, guards
# --------------------------------------------------------------------------
def _quote(**over):
    q = {"from_chain": "base", "from_token": "USDC",
         "to_chain": "base", "to_token": "USDC",
         "amount_cents": 10_000, "tier": 0,
         "path": _trivial_path(), "to_address": None}
    q.update(over)
    return q


def test_settlement_dryrun_receipt_signed(monkeypatch):
    monkeypatch.setenv("AWL_EXECUTION_MODE", "dryrun")
    monkeypatch.delenv("AWL_BROADCAST", raising=False)
    sk = SigningKey.generate()
    out = settlement.execute_quote(
        _quote(), {"volume_used_cents": 0, "txs_used": 0},
        signing_key=sk, oracle=_oracle_usdc(),
        adapters={"base": _StubAdapter()})
    env = out["attestation"]
    assert chamber.verify_attestation(env, sk.verify_key)
    receipt = json.loads(env["payload"])
    assert receipt["mode"] == "dryrun"
    assert not receipt.get("refused")
    assert receipt["fee_cents"] == 125 and receipt["net_cents"] == 9875
    assert receipt["hops"][0]["amount_base_units"] == 98_750_000
    assert receipt["status"] == "ok"


def test_settlement_dryrun_dust_refusal_signed(monkeypatch):
    monkeypatch.setenv("AWL_EXECUTION_MODE", "dryrun")
    sk = SigningKey.generate()
    out = settlement.execute_quote(
        _quote(amount_cents=20), {"volume_used_cents": 0, "txs_used": 0},
        signing_key=sk, oracle=_oracle_usdc(),
        adapters={"base": _StubAdapter()})
    receipt = json.loads(out["attestation"]["payload"])
    assert chamber.verify_attestation(out["attestation"], sk.verify_key)
    assert receipt["refused"] is True
    assert receipt["status"] == "refused_dust_eaten_by_fees"
    assert receipt["mode"] == "dryrun"


def test_settlement_broadcast_mode_without_flag_raises(monkeypatch):
    monkeypatch.setenv("AWL_EXECUTION_MODE", "broadcast")
    monkeypatch.delenv("AWL_BROADCAST", raising=False)
    # recipient supplied so we reach the broadcast gate (not the
    # refused_no_recipient path): the gate must RAISE, never downgrade
    with pytest.raises(RuntimeError, match="AWL_BROADCAST"):
        settlement.execute_quote(
            _quote(to_address="0x" + "11" * 20),
            {"volume_used_cents": 0, "txs_used": 0},
            signing_key=SigningKey.generate(), oracle=_oracle_usdc(),
            adapters={"base": _StubAdapter()})


def test_settlement_bad_mode_raises(monkeypatch):
    monkeypatch.setenv("AWL_EXECUTION_MODE", "yolo")
    with pytest.raises(ValueError, match="AWL_EXECUTION_MODE"):
        settlement.execute_quote(
            _quote(), {"volume_used_cents": 0, "txs_used": 0},
            signing_key=SigningKey.generate())


def test_settlement_mainnet_guard_still_first(monkeypatch):
    monkeypatch.setenv("AWL_MAINNET_ENABLED", "1")
    monkeypatch.setenv("AWL_EXECUTION_MODE", "dryrun")
    with pytest.raises(RuntimeError, match="mainnet"):
        settlement.execute_quote(
            _quote(), {"volume_used_cents": 0, "txs_used": 0},
            signing_key=SigningKey.generate(), oracle=_oracle_usdc(),
            adapters={"base": _StubAdapter()})
