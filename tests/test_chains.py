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

try:
    import solders  # noqa: F401
    _SOLDERS = True
except ImportError:
    _SOLDERS = False

needs_solders = pytest.mark.skipif(
    not _SOLDERS,
    reason="solders is an optional Solana dep, intentionally not installed in CI",
)

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
@needs_solders
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


@needs_solders
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


@needs_solders
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
    assert receipt["fee_cents"] == 50 and receipt["net_cents"] == 9950
    assert receipt["hops"][0]["amount_base_units"] == 99_500_000
    assert receipt["status"] == "ok"


def test_settlement_dryrun_dust_refusal_signed(monkeypatch):
    monkeypatch.setenv("AWL_EXECUTION_MODE", "dryrun")
    sk = SigningKey.generate()
    out = settlement.execute_quote(
        _quote(amount_cents=0), {"volume_used_cents": 0, "txs_used": 0},
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


# --------------------------------------------------------------------------
# Toll settlement send path (Base mainnet USDC) — unit tests with a fake
# chain client. No test here ever touches a live network: _w3client is
# stubbed, and the toll gate + caps + idempotency are exercised for real.
# --------------------------------------------------------------------------

_TOLL_SEED_HEX = "ab" * 32  # throwaway test key — never a real wallet
_TOLL_TO = "0x" + "cd" * 20


class _FakeEth:
    def __init__(self):
        self.chain_id = 8453
        self.sent = []
        self.known = {}  # tx_hash -> tx dict, as if seen on chain

    def get_transaction_count(self, addr):
        return 7

    def get_transaction(self, h):
        return self.known.get(h)

    @property
    def gas_price(self):
        return 1_000_000_000

    def send_raw_transaction(self, raw: bytes):
        assert isinstance(raw, bytes) and len(raw) > 0
        h = "0x%064x" % (0x70E1 + len(self.sent))
        self.sent.append(bytes(raw))
        return bytes.fromhex(h[2:])


class _FakeW3:
    def __init__(self):
        self.eth = _FakeEth()


@pytest.fixture()
def toll_env(monkeypatch, tmp_path):
    monkeypatch.setenv("TOLL_MAINNET_AUTHORIZED", "1")
    monkeypatch.setenv("TOLL_RPC_BASE", "http://127.0.0.1:9/")  # unused: stubbed
    monkeypatch.setenv("TOLL_ESCROW_KEY", _TOLL_SEED_HEX)
    monkeypatch.setenv("TOLL_BONDS_KEY", _TOLL_SEED_HEX)
    monkeypatch.setenv("TOLL_STATE", str(tmp_path / "toll-state.json"))
    monkeypatch.setenv("TOLL_LEDGER", str(tmp_path / "toll-ledger.jsonl"))
    fake = _FakeW3()
    monkeypatch.setattr(chains.EvmAdapter, "_w3client",
                        lambda self: fake)
    return fake


def test_toll_send_happy_path(toll_env, tmp_path):
    out = chains.toll_send_usdc("escrow", _TOLL_TO, 1_000_000, "key-1")
    assert out["idempotent"] is False
    assert out["tx_hash"].startswith("0x") and len(out["tx_hash"]) == 66
    assert len(toll_env.eth.sent) == 1
    # ledger line appended
    lines = (tmp_path / "toll-ledger.jsonl").read_text().strip().split("\n")
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["slot"] == "escrow" and entry["amount_uusdc"] == 1_000_000
    assert entry["tx_hash"] == out["tx_hash"] and entry["chain_id"] == 8453


def test_toll_send_idempotent_retry(toll_env):
    first = chains.toll_send_usdc("escrow", _TOLL_TO, 1_000_000, "key-dup")
    second = chains.toll_send_usdc("escrow", _TOLL_TO, 1_000_000, "key-dup")
    assert second["idempotent"] is True
    assert second["tx_hash"] == first["tx_hash"]
    assert len(toll_env.eth.sent) == 1  # no double-send


def test_toll_send_per_tx_cap(toll_env, monkeypatch):
    monkeypatch.setattr(chains, "TOLL_MAX_TX_UUSDC", 500_000)
    with pytest.raises(RuntimeError, match="per-tx cap"):
        chains.toll_send_usdc("escrow", _TOLL_TO, 500_001, "key-cap")
    assert len(toll_env.eth.sent) == 0


def test_toll_send_daily_cap(toll_env, monkeypatch):
    monkeypatch.setattr(chains, "TOLL_MAX_DAILY_UUSDC", 1_500_000)
    chains.toll_send_usdc("escrow", _TOLL_TO, 1_000_000, "key-d1")
    with pytest.raises(RuntimeError, match="daily toll cap"):
        chains.toll_send_usdc("escrow", _TOLL_TO, 1_000_000, "key-d2")
    assert len(toll_env.eth.sent) == 1


def test_toll_send_needs_authorization(toll_env, monkeypatch):
    monkeypatch.setenv("TOLL_MAINNET_AUTHORIZED", "0")
    with pytest.raises(RuntimeError, match="TOLL_MAINNET_AUTHORIZED"):
        chains.toll_send_usdc("escrow", _TOLL_TO, 1_000_000, "key-noauth")
    assert len(toll_env.eth.sent) == 0


def test_toll_send_needs_rpc(toll_env, monkeypatch):
    monkeypatch.delenv("TOLL_RPC_BASE")
    with pytest.raises(RuntimeError, match="TOLL_RPC_BASE"):
        chains.toll_send_usdc("escrow", _TOLL_TO, 1_000_000, "key-norpc")
    assert len(toll_env.eth.sent) == 0


def test_toll_broadcast_rejects_non_toll_adapter(toll_env):
    adapter = chains.EvmAdapter("base")  # testnet config, no toll marker
    with pytest.raises(RuntimeError, match="not toll-authorized"):
        adapter.broadcast_toll_mainnet("0x" + "ab" * 32)
    assert len(toll_env.eth.sent) == 0


def test_generic_broadcast_still_refuses_mainnet_cfg(toll_env):
    # Even handed the toll config, the GENERIC broadcast path must refuse:
    # broadcast_allowed() never opens mainnet.
    cfg = chains.assert_toll_mainnet()
    adapter = chains.EvmAdapter("base", cfg_override=cfg)
    with pytest.raises(RuntimeError):
        adapter.broadcast("0x" + "ab" * 32)
    assert len(toll_env.eth.sent) == 0


# --------------------------------------------------------------------------
# Toll TESTNET path (Base Sepolia 84532). Stubbed chain client; no live
# network. Proves the sepolia/mainnet separation: separate flags, separate
# configs, separate state — the two can never cross.
# --------------------------------------------------------------------------

class _FakeEthSepolia(_FakeEth):
    def __init__(self):
        super().__init__()
        self.chain_id = 84532


class _FakeW3Sepolia(_FakeW3):
    def __init__(self):
        self.eth = _FakeEthSepolia()


@pytest.fixture()
def testnet_env(monkeypatch, tmp_path):
    monkeypatch.setenv("TOLL_TESTNET_SEPOLIA", "1")
    monkeypatch.setenv("TOLL_RPC_SEPOLIA", "http://127.0.0.1:9/")
    monkeypatch.setenv("TOLL_TESTNET_ESCROW_KEY", "ab" * 32)
    monkeypatch.setenv("TOLL_TESTNET_BONDS_KEY", "cd" * 32)
    monkeypatch.setenv("TOLL_TESTNET_STATE", str(tmp_path / "tn-state.json"))
    monkeypatch.setenv("TOLL_TESTNET_LEDGER",
                       str(tmp_path / "tn-ledger.jsonl"))
    # mainnet must stay OFF here: proves the testnet path doesn't need it
    monkeypatch.setenv("TOLL_MAINNET_AUTHORIZED", "0")
    fake = _FakeW3Sepolia()
    monkeypatch.setattr(chains.EvmAdapter, "_w3client",
                        lambda self: fake)
    return fake


def test_toll_testnet_send_happy_path(testnet_env, tmp_path):
    out = chains.toll_send_usdc("escrow", _TOLL_TO, 500_000, "tn-key-1",
                                network="sepolia")
    assert out["idempotent"] is False
    assert out["tx_hash"].startswith("0x")
    assert len(testnet_env.eth.sent) == 1
    lines = (tmp_path / "tn-ledger.jsonl").read_text().strip().split("\n")
    entry = json.loads(lines[0])
    assert entry["chain_id"] == 84532
    assert entry["network"] == "sepolia"
    assert entry["token"] == "0x036CbD53842c5426634e7929541eC2318f3dCF7e"


def test_toll_testnet_needs_flag(testnet_env, monkeypatch):
    monkeypatch.setenv("TOLL_TESTNET_SEPOLIA", "0")
    with pytest.raises(RuntimeError, match="TOLL_TESTNET_SEPOLIA"):
        chains.toll_send_usdc("escrow", _TOLL_TO, 500_000, "tn-noflag",
                              network="sepolia")
    assert len(testnet_env.eth.sent) == 0


def test_toll_testnet_cross_refusal(testnet_env, monkeypatch):
    # mainnet config must REFUSE the testnet broadcast, and vice versa.
    monkeypatch.setenv("TOLL_MAINNET_AUTHORIZED", "1")
    monkeypatch.setenv("TOLL_RPC_BASE", "http://127.0.0.1:9/")
    monkeypatch.setenv("TOLL_ESCROW_KEY", "ab" * 32)
    main_cfg = chains.assert_toll_mainnet()
    test_cfg = chains.assert_toll_testnet()
    main_adapter = chains.EvmAdapter("base", cfg_override=main_cfg)
    test_adapter = chains.EvmAdapter("base", cfg_override=test_cfg)
    with pytest.raises(RuntimeError, match="not testnet-toll-authorized"):
        main_adapter.broadcast_toll_testnet("0x" + "ab" * 32)
    with pytest.raises(RuntimeError, match="not toll-authorized"):
        test_adapter.broadcast_toll_mainnet("0x" + "ab" * 32)
    assert len(testnet_env.eth.sent) == 0


def test_toll_testnet_state_isolation(testnet_env, tmp_path, monkeypatch):
    # Same idempotency key on both networks: each sends once (separate
    # state files), proving no cross-network replay or dedup.
    out_tn = chains.toll_send_usdc("escrow", _TOLL_TO, 100_000, "shared-key",
                                   network="sepolia")
    assert out_tn["idempotent"] is False
    # Now swap in the mainnet stub (chain 8453) with mainnet auth.
    fake_mn = _FakeW3()
    monkeypatch.setattr(chains.EvmAdapter, "_w3client",
                        lambda self: fake_mn)
    monkeypatch.setenv("TOLL_MAINNET_AUTHORIZED", "1")
    monkeypatch.setenv("TOLL_RPC_BASE", "http://127.0.0.1:9/")
    monkeypatch.setenv("TOLL_ESCROW_KEY", "ab" * 32)
    monkeypatch.setenv("TOLL_STATE", str(tmp_path / "mn-state.json"))
    monkeypatch.setenv("TOLL_LEDGER", str(tmp_path / "mn-ledger.jsonl"))
    out_mn = chains.toll_send_usdc("escrow", _TOLL_TO, 100_000, "shared-key",
                                   network="mainnet")
    assert out_mn["idempotent"] is False
    assert len(fake_mn.eth.sent) == 1
    # Separate state files: the key is consumed once per network.
    tn_state = json.loads((tmp_path / "tn-state.json").read_text())
    mn_state = json.loads((tmp_path / "mn-state.json").read_text())
    assert "shared-key" in tn_state["consumed"]
    assert "shared-key" in mn_state["consumed"]


# --------------------------------------------------------------------------
# Crash-safe idempotency: the reserve-before-broadcast design.
# A crash between the durable reserve and the broadcast must reconcile
# against the chain on retry — never build a second transaction.
# --------------------------------------------------------------------------

def _write_pending_state(tmp_path, key, tx_hash, raw_tx, amount=1_000_000):
    state_path = tmp_path / "toll-state.json"
    state = {"consumed": {key: {
        "status": "pending", "tx_hash": tx_hash, "raw_tx": raw_tx,
        "slot": "escrow", "to": _TOLL_TO, "amount_uusdc": amount,
        "nonce": 7, "day": "2026-09-29"}}, "daily": {}}
    state_path.write_text(json.dumps(state))
    return state_path


def test_toll_send_crash_recovery_tx_landed(toll_env, tmp_path):
    # Crash AFTER the durable reserve, tx DID land: retry must adopt it
    # and never broadcast again.
    tx_hash = "0x" + "ab" * 32
    raw_tx = "02" + "cd" * 100
    state_path = _write_pending_state(tmp_path, "key-crash-1", tx_hash, raw_tx)
    toll_env.eth.known[tx_hash] = {"hash": tx_hash}  # node knows it: landed
    out = chains.toll_send_usdc("escrow", _TOLL_TO, 1_000_000, "key-crash-1")
    assert out["idempotent"] is True
    assert out["tx_hash"] == tx_hash
    assert len(toll_env.eth.sent) == 0  # never re-sent
    state2 = json.loads(state_path.read_text())
    assert state2["consumed"]["key-crash-1"]["status"] == "sent"


def test_toll_send_crash_recovery_rebroadcast_identical(toll_env, tmp_path):
    # Crash BEFORE broadcast: tx never hit the chain. Retry must
    # re-broadcast the IDENTICAL bytes — never build a new tx, so a
    # double-send is impossible by construction.
    tx_hash = "0x" + "ef" * 32
    raw_tx = "02" + "aa" * 100
    state_path = _write_pending_state(tmp_path, "key-crash-2", tx_hash,
                                      raw_tx, amount=2_000_000)
    # node does NOT know the hash: never landed
    out = chains.toll_send_usdc("escrow", _TOLL_TO, 2_000_000, "key-crash-2")
    assert out["idempotent"] is False
    assert out["tx_hash"] == tx_hash
    assert len(toll_env.eth.sent) == 1
    assert toll_env.eth.sent[0] == bytes.fromhex(raw_tx)  # identical bytes
    state2 = json.loads(state_path.read_text())
    assert state2["consumed"]["key-crash-2"]["status"] == "sent"


def test_toll_send_legacy_string_record(toll_env, tmp_path):
    # Pre-crash-safe records (plain tx-hash strings) still replay clean.
    state_path = tmp_path / "toll-state.json"
    state_path.write_text(json.dumps(
        {"consumed": {"key-old": "0x" + "12" * 32}, "daily": {}}))
    out = chains.toll_send_usdc("escrow", _TOLL_TO, 1_000_000, "key-old")
    assert out["idempotent"] is True
    assert out["tx_hash"] == "0x" + "12" * 32
    assert len(toll_env.eth.sent) == 0
