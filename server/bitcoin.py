#!/usr/bin/env python3
"""awLPay Bitcoin rail — receive-side helpers for x402 payments in BTC.

HARD RULES for this module:

  * READ-ONLY verification. This module never signs, never sends
    transactions, never holds keys. It only READS chain state from a
    public Esplora endpoint (Blockstream, mempool.space fallback).
    The E2E test path (tests/manual/) is the only place a throwaway
    testnet wallet signs, and it never touches mainnet.
  * Amounts are integer SATOSHIS end-to-end (1 BTC = 100_000_000 sats).
    Never floats: the fee-rate screen uses the integer comparison
    fee >= vsize (i.e. >= 1 sat/vB), never fee/vsize as a float.
  * STDLIB ONLY. Address validation (bech32/bech32m, base58check) and
    "Bitcoin Signed Message" compact-signature recovery are implemented
    here in pure stdlib — the only outside code is the lab's own
    vendored secp256k1 in server/ethsig.py (the same ecrecover the
    x402 EVM gate uses). embit is used by the TESTS to generate
    cross-implementation vectors; it is NOT a module dependency.
  * TESTNET (signet) is the default posture. Mainnet
    ("bip122:000000000019d6689c085ae165831e934") is recognized as a
    network id with public Esplora defaults so the verifier can name
    it in refusals, but under the RAIL ROLLOUT standing order mainnet
    goes live only after signet greens, with Corey's explicit
    per-charge approval — never a config flip.

CONFIRMATION POLICY (two-tier — documented here, enforced in
verify_btc_payment):

  (a) CONFIRMED: status.confirmed is true -> confirmations =
      tip_height (GET /blocks/tip/height) - block_height + 1 >= 1
      -> accept.
  (b) 0-CONF (zero-confirmation): accepted ONLY if NO input signals
      BIP-125 replace-by-fee (any vin with sequence < 0xfffffffe) AND
      the effective fee rate is >= 1 sat/vB (fee >= vsize, integer
      compare). RBF-signaling or sub-1-sat/vB mempool txs are refused.
      vsize comes from the tx's "vsize" field when the Esplora has it
      (mempool.space); Blockstream's Esplora omits it, so it is derived
      as ceil(weight/4), falling back to "size" (conservative).

Env (read at call time):
    AWL_PAY_TO_BTC      receiving address (unset disables the BTC rail
                        in x402.payment_terms)
    AWL_RPC_BTC         comma-separated mainnet Esplora base URLs
                        (default https://blockstream.info/api)
    AWL_RPC_BTC_SIGNET  comma-separated signet Esplora base URLs
                        (default https://blockstream.info/signet/api)
    (each list falls back to https://mempool.space/api when set but
    empty of entries; mempool.space has no signet mirror, so a custom
    signet list with no entries is an error)
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import threading
import urllib.error
import urllib.request

from server import ethsig

# --------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------

# CAIP-2 bip122 ids. Mainnet id is the form given by the rail spec
# (genesis hash truncated to 32 hex chars); signet id resolved
# 2026-10-02 from https://blockstream.info/signet/api/block-height/0
# (full signet genesis:
#  00000008819873e925422c1ff0f99f7cc9bbb232af63a077a480a3633bee1ef6).
BTC_MAINNET = "bip122:000000000019d6689c085ae165831e934"
BTC_SIGNET = "bip122:00000008819873e925422c1ff0f99f7cc9bbb"

SATS_PER_BTC = 100_000_000

MAINNET_RPC_DEFAULT = ["https://blockstream.info/api"]
SIGNET_RPC_DEFAULT = ["https://blockstream.info/signet/api"]
FALLBACK_RPC = "https://mempool.space/api"

# Bitcoin txids are 64 lowercase hex chars on Esplora. Accept uppercase
# input too, but always normalize to lowercase (canonical form).
TXID_RE = re.compile(r"^[0-9A-Fa-f]{64}$")

# BIP-125: a tx is RBF-signaling if any input has sequence < 0xfffffffe.
RBF_SIGNAL_MAX = 0xFFFFFFFE

# bech32 address anatomy
_BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_BECH32_CONST = 1
_BECH32M_CONST = 0x2BC830A3

_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

# base58 version bytes: P2PKH / P2SH per network
_B58_VERSIONS = {
    BTC_MAINNET: (0x00, 0x05),
    BTC_SIGNET: (0x6F, 0xC4),
}
# bech32 human-readable parts per network
_BECH32_HRPS = {
    BTC_MAINNET: "bc",
    BTC_SIGNET: "tb",
}


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

def btc_pay_to() -> str:
    return os.environ.get("AWL_PAY_TO_BTC", "").strip()


def _env_list(name: str) -> list[str]:
    return [u.strip() for u in os.environ.get(name, "").split(",")
            if u.strip()]


def btc_rpcs() -> list[str]:
    return _env_list("AWL_RPC_BTC") or list(MAINNET_RPC_DEFAULT)


def btc_signet_rpcs() -> list[str]:
    return _env_list("AWL_RPC_BTC_SIGNET") or list(SIGNET_RPC_DEFAULT)


def rpc_urls_for(network: str) -> list[str]:
    if network == BTC_MAINNET:
        return btc_rpcs() + [FALLBACK_RPC]
    if network == BTC_SIGNET:
        return btc_signet_rpcs()
    return []


# --------------------------------------------------------------------------
# pure helpers
# --------------------------------------------------------------------------

def valid_txid(h) -> bool:
    return bool(isinstance(h, str) and TXID_RE.fullmatch(h.strip()))


def norm_txid(h: str) -> str:
    """Canonical lowercase txid. Raises ValueError on bad format."""
    h = (h or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", h):
        raise ValueError("bad txid format (want 64 hex chars)")
    return h


def sats_to_int(sats) -> int:
    """Strict sats -> int. Raises on anything non-integer."""
    s = str(sats).strip()
    if not re.fullmatch(r"[0-9]+", s):
        raise ValueError("not an integer sats value: %r" % (sats,))
    return int(s)


def btc_to_sats_str(btc: str) -> str:
    """'0.00000001' -> '1'. String in, string out, no floats."""
    from decimal import Decimal
    return str(int(Decimal(str(btc).strip()) * SATS_PER_BTC))


def binding_message(tx_hash: str, resource: str) -> bytes:
    """Canonical message the payer signs to bind a txHash proof to their
    address and to the exact endpoint being called.

    DUPLICATED (not imported) from x402.py's binding_message: the format
    must stay byte-identical for cross-rail compatibility, and importing
    x402 from this leaf module would create a server-package cycle.
    Cross-checked by tests/test_bitcoin.py::test_binding_matches_x402."""
    return ("awlpay payment proof\n"
            "txHash: %s\n"
            "resource: %s" % (tx_hash.strip().lower(),
                              resource.strip())).encode()


# --------------------------------------------------------------------------
# bech32 / bech32m (BIP-173, BIP-350) — stdlib only
# --------------------------------------------------------------------------

def _bech32_polymod(values) -> int:
    gen = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    chk = 1
    for v in values:
        b = chk >> 25
        chk = ((chk & 0x1FFFFFF) << 5) ^ v
        for i in range(5):
            if (b >> i) & 1:
                chk ^= gen[i]
    return chk


def _bech32_hrp_expand(hrp: str) -> list[int]:
    return [ord(x) >> 5 for x in hrp] + [0] + [ord(x) & 31 for x in hrp]


def _bech32_checksum_kind(hrp: str, data: list[int]):
    """'bech32', 'bech32m', or None. data includes the 6-char checksum."""
    chk = _bech32_polymod(_bech32_hrp_expand(hrp) + data)
    if chk == _BECH32_CONST:
        return "bech32"
    if chk == _BECH32M_CONST:
        return "bech32m"
    return None


def bech32_decode(addr):
    """Decode a segwit-style address.

    Returns (hrp, version, program_bytes, kind) where kind is 'bech32'
    or 'bech32m', or None if the string is not a well-formed bech32 /
    bech32m string. Network matching is the caller's job (see
    valid_btc_address).
    """
    if not isinstance(addr, str):
        return None
    if len(addr) < 8 or len(addr) > 90:
        return None
    # BIP-173: no mixed case.
    if addr != addr.lower() and addr != addr.upper():
        return None
    addr = addr.lower()
    if not all(33 <= ord(c) <= 126 for c in addr):
        return None
    pos = addr.rfind("1")
    if pos < 1 or pos + 7 > len(addr):
        return None
    hrp = addr[:pos]
    try:
        data = [_BECH32_CHARSET.index(c) for c in addr[pos + 1:]]
    except ValueError:
        return None
    kind = _bech32_checksum_kind(hrp, data)
    if kind is None:
        return None
    payload = data[:-6]
    if not payload:
        # Degenerate but checksum-valid (e.g. BIP-173's "A12UEL5L"):
        # not a segwit address — version None marks it as such.
        return hrp, None, b"", kind
    version = payload[0]
    # convertbits(payload[1:], 5 -> 8, no padding). Like the BIP-173
    # reference decoder, zero-padding is NOT enforced here — that rule
    # lives at the segwit-address layer, not in bech32 itself.
    acc = 0
    bits = 0
    program = bytearray()
    for v in payload[1:]:
        acc = (acc << 5) | v
        bits += 5
        while bits >= 8:
            bits -= 8
            program.append((acc >> bits) & 0xFF)
    return hrp, version, bytes(program), kind


# --------------------------------------------------------------------------
# base58check — stdlib only
# --------------------------------------------------------------------------

def _base58_to_int(s: str):
    n = 0
    for c in s:
        d = _B58_ALPHABET.find(c)
        if d < 0:
            return None
        n = n * 58 + d
    return n


def base58check_decode(addr):
    """Decode a base58check address. Returns the 21-byte
    version+hash160 payload, or None."""
    if not isinstance(addr, str) or not addr:
        return None
    n = _base58_to_int(addr)
    if n is None:
        return None
    raw = n.to_bytes(max(1, (n.bit_length() + 7) // 8), "big")
    pad = 0
    for c in addr:
        if c == "1":
            pad += 1
        else:
            break
    full = b"\x00" * pad + raw
    if len(full) != 25:
        return None
    if _sha256d(full[:21])[:4] != full[21:]:
        return None
    return full[:21]


def base58check_encode(payload21: bytes) -> str:
    """Encode a 21-byte version+hash160 payload as base58check."""
    chk = _sha256d(payload21)[:4]
    n = int.from_bytes(payload21 + chk, "big")
    s = ""
    while n > 0:
        n, r = divmod(n, 58)
        s = _B58_ALPHABET[r] + s
    pad = 0
    for b in payload21 + chk:
        if b == 0:
            pad += 1
        else:
            break
    return "1" * pad + s


def _sha256d(b: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(b).digest()).digest()


# --------------------------------------------------------------------------
# RIPEMD-160: hashlib when available, pure-python fallback
# --------------------------------------------------------------------------

def _ripemd160_pure(data: bytes) -> bytes:
    """Pure-python RIPEMD-160 (used only when the OpenSSL build lacks
    ripemd160, e.g. OpenSSL >= 3.4 legacy-provider builds). Cross-checked
    against hashlib's in tests."""
    ml = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15,
          7, 4, 13, 1, 10, 6, 15, 3, 12, 0, 9, 5, 2, 14, 11, 8,
          3, 10, 14, 4, 9, 15, 8, 1, 2, 7, 0, 6, 13, 11, 5, 12,
          1, 9, 11, 10, 0, 8, 12, 4, 13, 3, 7, 15, 14, 5, 6, 2,
          4, 0, 5, 9, 7, 12, 2, 10, 14, 1, 3, 8, 11, 6, 15, 13]
    mr = [5, 14, 7, 0, 9, 2, 11, 4, 13, 6, 15, 8, 1, 10, 3, 12,
          6, 11, 3, 7, 0, 13, 5, 10, 14, 15, 8, 12, 4, 9, 1, 2,
          15, 5, 1, 3, 7, 14, 6, 9, 11, 8, 12, 2, 10, 0, 4, 13,
          8, 6, 4, 1, 3, 11, 15, 0, 5, 12, 2, 13, 9, 7, 10, 14,
          12, 15, 10, 4, 1, 5, 8, 7, 6, 2, 13, 14, 0, 3, 9, 11]
    rl = [11, 14, 15, 12, 5, 8, 7, 9, 11, 13, 14, 15, 6, 7, 9, 8,
          7, 6, 8, 13, 11, 9, 7, 15, 7, 12, 15, 9, 11, 7, 13, 12,
          11, 13, 6, 7, 14, 9, 13, 15, 14, 8, 13, 6, 5, 12, 7, 5,
          11, 12, 14, 15, 14, 15, 9, 8, 9, 14, 5, 6, 8, 6, 5, 12,
          9, 15, 5, 11, 6, 8, 13, 12, 5, 12, 13, 14, 11, 8, 5, 6]
    rr = [8, 9, 9, 11, 13, 15, 15, 5, 7, 7, 8, 11, 14, 14, 12, 6,
          9, 13, 15, 7, 12, 8, 9, 11, 7, 7, 12, 7, 6, 15, 13, 11,
          9, 7, 15, 11, 8, 6, 6, 14, 12, 13, 5, 14, 13, 13, 7, 5,
          15, 5, 8, 11, 14, 14, 6, 14, 6, 9, 12, 9, 12, 5, 15, 8,
          8, 5, 12, 9, 12, 5, 14, 6, 8, 13, 6, 5, 15, 13, 11, 11]
    kl = [0x00000000, 0x5A827999, 0x6ED9EBA1, 0x8F1BBCDC, 0xA953FD4E]
    kr = [0x50A28BE6, 0x5C4DD124, 0x6D703EF3, 0x7A6D76E9, 0x00000000]

    def rol(x, n):
        return ((x << n) | (x >> (32 - n))) & 0xFFFFFFFF

    def f(j, x, y, z):
        if j < 16:
            return x ^ y ^ z
        if j < 32:
            return (x & y) | (~x & z)
        if j < 48:
            return (x | ~y) ^ z
        if j < 64:
            return (x & z) | (y & ~z)
        return x ^ (y | ~z)

    msg = bytearray(data)
    msg.append(0x80)
    while len(msg) % 64 != 56:
        msg.append(0)
    msg += (8 * len(data)).to_bytes(8, "little")
    h0, h1, h2, h3, h4 = (0x67452301, 0xEFCDAB89, 0x98BADCFE,
                          0x10325476, 0xC3D2E1F0)
    for off in range(0, len(msg), 64):
        x = [int.from_bytes(msg[off + 4 * i:off + 4 * i + 4], "little")
             for i in range(16)]
        al, bl, cl, dl, el = h0, h1, h2, h3, h4
        ar, br, cr, dr, er = h0, h1, h2, h3, h4
        for j in range(80):
            t = (al + f(j, bl, cl, dl) + x[ml[j]] + kl[j // 16]) & 0xFFFFFFFF
            t = (rol(t, rl[j]) + el) & 0xFFFFFFFF
            al, el, dl, cl, bl = el, dl, rol(cl, 10), bl, t
            t = (ar + f(79 - j, br, cr, dr) + x[mr[j]]
                 + kr[j // 16]) & 0xFFFFFFFF
            t = (rol(t, rr[j]) + er) & 0xFFFFFFFF
            ar, er, dr, cr, br = er, dr, rol(cr, 10), br, t
        t = (h1 + cl + dr) & 0xFFFFFFFF
        h1 = (h2 + dl + er) & 0xFFFFFFFF
        h2 = (h3 + el + ar) & 0xFFFFFFFF
        h3 = (h4 + al + br) & 0xFFFFFFFF
        h4 = (h0 + bl + cr) & 0xFFFFFFFF
        h0 = t
    return b"".join(h.to_bytes(4, "little") for h in (h0, h1, h2, h3, h4))


def _ripemd160(data: bytes) -> bytes:
    try:
        h = hashlib.new("ripemd160", data)
        return h.digest()
    except (ValueError, TypeError):  # OpenSSL without the legacy provider
        return _ripemd160_pure(data)


def hash160(b: bytes) -> bytes:
    """RIPEMD-160(SHA-256(b)) — the Bitcoin address hash."""
    return _ripemd160(hashlib.sha256(b).digest())


# --------------------------------------------------------------------------
# address validation (network-aware)
# --------------------------------------------------------------------------

def valid_btc_address(addr, network) -> bool:
    """Validate a Bitcoin address for the given network.

    Segwit/bech32(m): hrp must be 'bc' (mainnet) or 'tb' (signet);
    version 0 -> bech32 checksum, 20- or 32-byte program;
    version 1..16 -> bech32m checksum, 20- or 32-byte program
    (v1 + 32 bytes = Taproot/P2TR).
    Base58: version byte 0x00/0x05 (mainnet P2PKH/P2SH) or
    0x6f/0xc4 (signet/testnet P2PKH/P2SH), with checksum.
    """
    if not isinstance(addr, str) or not addr:
        return False
    if network not in _B58_VERSIONS:
        return False
    dec = bech32_decode(addr)
    if dec is not None:
        hrp, version, program, kind = dec
        if hrp != _BECH32_HRPS[network]:
            return False  # parses, but wrong network
        if version is None:
            return False  # checksum-valid bech32, not a segwit address
        if len(program) not in (20, 32):
            return False
        if version == 0:
            return kind == "bech32"
        if 1 <= version <= 16:
            return kind == "bech32m"
        return False
    payload = base58check_decode(addr)
    if payload is None:
        return False
    return payload[0] in _B58_VERSIONS[network]


# --------------------------------------------------------------------------
# "Bitcoin Signed Message" compact-signature verification (stdlib)
# --------------------------------------------------------------------------

def _varint(n: int) -> bytes:
    if n < 0xFD:
        return bytes([n])
    if n <= 0xFFFF:
        return b"\xfd" + n.to_bytes(2, "little")
    if n <= 0xFFFFFFFF:
        return b"\xfe" + n.to_bytes(4, "little")
    return b"\xff" + n.to_bytes(8, "little")


def btc_message_hash(message: bytes) -> bytes:
    """Bitcoin message-signing digest: sha256d(varint(len(prefix)) +
    prefix + varint(len(msg)) + msg), prefix = 'Bitcoin Signed Message:\n'."""
    prefix = b"Bitcoin Signed Message:\n"
    preimage = _varint(len(prefix)) + prefix + _varint(len(message)) + message
    return _sha256d(preimage)


def _compress_pubkey(point) -> bytes:
    x, y = point
    return b"\x02" + x.to_bytes(32, "big") if y % 2 == 0 \
        else b"\x03" + x.to_bytes(32, "big")


def _uncompress_pubkey(point) -> bytes:
    x, y = point
    return b"\x04" + x.to_bytes(32, "big") + y.to_bytes(32, "big")


def verify_btc_message(address: str, signature_base64: str,
                       message: bytes, network: str) -> bool:
    """Verify a 'Bitcoin Signed Message' compact signature.

    signature_base64: base64 of the 65-byte compact signature
    (header byte 27..34 = 27 + recid + 4*compressed, then r || s).
    Returns True iff the recovered key's P2PKH address equals `address`.
    Never raises on malformed input — returns False.
    """
    try:
        if not isinstance(message, (bytes, bytearray)):
            return False
        if not valid_btc_address(address, network):
            return False
        raw = base64.b64decode(signature_base64, validate=True)
        if len(raw) != 65:
            return False
        header = raw[0]
        if not (27 <= header <= 34):
            return False
        recid = (header - 27) & 3
        compressed = bool((header - 27) & 4)
        r = int.from_bytes(raw[1:33], "big")
        s = int.from_bytes(raw[33:65], "big")
        # low-s malleability guard (mirrors ethsig.verify_personal_sign)
        if s > ethsig._N // 2:
            return False
        q = ethsig.ecrecover(btc_message_hash(bytes(message)), recid, r, s)
        if q is None:
            return False
        pub = _compress_pubkey(q) if compressed else _uncompress_pubkey(q)
        derived = base58check_encode(
            bytes([_B58_VERSIONS[network][0]]) + hash160(pub))
        a = address.strip()
        if len(a) != len(derived):
            return False
        diff = 0
        for x, y in zip(a.encode(), derived.encode()):
            diff |= x ^ y
        return diff == 0
    except (ValueError, TypeError, binascii.Error):
        return False


# --------------------------------------------------------------------------
# test seam: injectable Esplora (mirrors xrpl.set_test_rpc)
# --------------------------------------------------------------------------

_test_rpc = None
_test_rpc_lock = threading.Lock()


def set_test_rpc(fn) -> None:
    global _test_rpc
    with _test_rpc_lock:
        _test_rpc = fn


def clear_test_rpc() -> None:
    global _test_rpc
    with _test_rpc_lock:
        _test_rpc = None


def get_test_rpc():
    with _test_rpc_lock:
        return _test_rpc


# --------------------------------------------------------------------------
# Esplora REST (stdlib urllib GET — proxy-safe, sync, matches xrpl.py style)
# --------------------------------------------------------------------------

def _esplora_get(base: str, path: str, timeout: int = 25):
    url = base.rstrip("/") + "/" + path.lstrip("/")
    req = urllib.request.Request(
        url, headers={"User-Agent": "awlpay-btc/1.0",
                      "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None  # unknown tx / unknown path: not an error here
        raise


def _esplora_any(path: str, urls: list[str], timeout: int = 25):
    last = None
    for u in urls:
        try:
            return _esplora_get(u, path, timeout)
        except Exception as e:  # noqa: BLE001 - try next endpoint
            last = e
    raise last if last else RuntimeError("no btc esplora urls configured")


def fetch_tx(txid: str, urls: list[str] | None = None,
             timeout: int = 25):
    """Fetch a tx by id from Esplora. Returns the parsed tx dict, or
    None if Esplora 404s (unknown tx). Test seam: set_test_rpc(fn)
    replaces the network; fn takes a path like 'tx/<txid>'."""
    test_rpc = get_test_rpc()
    if test_rpc is not None:
        return test_rpc("tx/" + txid)
    return _esplora_any("tx/" + txid, urls or btc_rpcs(), timeout)


def fetch_tip_height(urls: list[str] | None = None, timeout: int = 25) -> int:
    """Current chain tip height. Test seam: fn('blocks/tip/height')."""
    test_rpc = get_test_rpc()
    if test_rpc is not None:
        return int(test_rpc("blocks/tip/height"))
    return int(_esplora_any("blocks/tip/height", urls or btc_rpcs(),
                            timeout))


# --------------------------------------------------------------------------
# verification (the §2 checklist, BTC-native)
# --------------------------------------------------------------------------

def _clean_fail(reason: str, **kw) -> tuple[bool, dict]:
    info = {"reason": reason}
    info.update(kw)
    return False, info


def _int_field(obj, name):
    v = obj.get(name)
    if isinstance(v, bool) or not isinstance(v, int):
        return None
    return v


def verify_btc_payment(tx_hash: str, network: str, min_sats: int,
                       pay_to: str, used_set: set,
                       rpc=None, payer_sig: str | None = None,
                       resource: str = "", timeout: int = 25
                       ) -> tuple[bool, dict]:
    """Verify a Bitcoin payment. Returns (ok, info). READ-ONLY: never
    mutates used_set — the caller consumes the replay key after success.

    tx_hash: 64-hex txid (case-insensitive, normalized to lowercase).
    network: BTC_MAINNET or BTC_SIGNET (bip122 ids).
    min_sats: minimum acceptable amount, integer satoshis.
    pay_to: the challenged destination address (network-checked).
    used_set: replay store (checked read-only).
    rpc: optional path -> result callable (tests); paths look like
        'tx/<txid>' and 'blocks/tip/height'.
    payer_sig: REQUIRED — base64 compact 'Bitcoin Signed Message'
        signature over binding_message(tx_hash, resource) by one of the
        tx's input addresses (anti-front-running binding). None fails.
    resource: the https URL being called (binds the signature).
    """
    try:
        h = norm_txid(tx_hash)
    except ValueError:
        return _clean_fail("bad txid format (want 64 hex chars)")
    if network not in (BTC_MAINNET, BTC_SIGNET):
        return _clean_fail("unsupported network %r (btc rail takes "
                           "%r mainnet or %r signet)"
                           % (network, BTC_MAINNET, BTC_SIGNET))
    key = network + ":" + h
    if key in used_set:
        return _clean_fail("replay: payment already used", replay_key=key)
    if not valid_btc_address(pay_to, network):
        return _clean_fail("bad payTo address %r for network %s"
                           % (pay_to, network))
    try:
        min_sats = int(min_sats)
    except (ValueError, TypeError):
        return _clean_fail("bad min_sats %r" % (min_sats,))
    if min_sats <= 0:
        return _clean_fail("min_sats must be positive")
    if payer_sig is None:
        return _clean_fail("missing payerSig: the payer must sign "
                           "binding_message(txHash, resource) with a "
                           "'Bitcoin Signed Message' compact signature "
                           "from one of the tx's input addresses",
                           replay_key=key)

    # --- fetch ---
    urls = rpc_urls_for(network)
    try:
        if rpc is not None:
            tx = rpc("tx/" + h)
        else:
            tx = fetch_tx(h, urls=urls, timeout=timeout)
    except Exception as e:  # noqa: BLE001 - surfaced as clean failure
        return _clean_fail("btc esplora unreachable: %s" % str(e)[:120],
                           replay_key=key)
    if not tx:
        return _clean_fail("tx not found", replay_key=key)

    # --- amount: sum of vouts paying pay_to (integer sats) ---
    vouts = tx.get("vout") or []
    paid_sats = 0
    for v in vouts:
        if not isinstance(v, dict):
            continue
        if v.get("scriptpubkey_address") == pay_to:
            try:
                paid_sats += sats_to_int(v.get("value"))
            except (ValueError, TypeError):
                return _clean_fail("bad vout value %r" % (v.get("value"),),
                                   replay_key=key)
    if paid_sats < min_sats:
        return _clean_fail("underpaid: got %d sats, need %d"
                           % (paid_sats, min_sats),
                           paid_sats=paid_sats, replay_key=key)

    # --- confirmation policy (two-tier) ---
    status = tx.get("status") or {}
    confirmations = 0
    if status.get("confirmed"):
        block_height = _int_field(status, "block_height")
        if block_height is None:
            return _clean_fail("confirmed tx has no block_height",
                               replay_key=key)
        try:
            tip = int(rpc("blocks/tip/height") if rpc is not None
                      else fetch_tip_height(urls=urls, timeout=timeout))
        except Exception as e:  # noqa: BLE001
            return _clean_fail("could not read chain tip: %s" % str(e)[:80],
                               replay_key=key)
        confirmations = tip - block_height + 1
        if confirmations < 1:
            return _clean_fail("tx claims confirmed but block is above "
                               "the tip", replay_key=key)
    else:
        # 0-CONF path: BIP-125 RBF screen + minimum fee rate.
        vins = tx.get("vin") or []
        if not vins:
            return _clean_fail("tx has no inputs", replay_key=key)
        for vin in vins:
            if not isinstance(vin, dict):
                continue
            seq = _int_field(vin, "sequence")
            if seq is None or seq < RBF_SIGNAL_MAX:
                return _clean_fail("unconfirmed and RBF-signaling: "
                                   "refusing 0-conf (wait for a block)",
                                   replay_key=key)
        fee = _int_field(tx, "fee")
        # vsize: mempool.space's Esplora returns it; Blockstream's does
        # not — derive from weight (vsize = ceil(weight/4)), falling back
        # to size (conservative: size >= vsize, so fee >= size still
        # implies >= 1 sat/vB; it can only over-refuse, never under-).
        vsize = _int_field(tx, "vsize")
        if vsize is None:
            weight = _int_field(tx, "weight")
            if weight is not None:
                vsize = (weight + 3) // 4
            else:
                vsize = _int_field(tx, "size")
        if fee is None or vsize is None or vsize <= 0:
            return _clean_fail("unconfirmed tx has no usable fee/vsize",
                               replay_key=key)
        # Integer-only: fee >= vsize  <=>  >= 1 sat/vB.
        if fee < vsize:
            return _clean_fail("fee too low for 0-conf: %d sats on %d "
                               "vB (< 1 sat/vB)" % (fee, vsize),
                               replay_key=key)

    # --- payer binding: signature must verify against one input address ---
    msg = binding_message(h, resource)
    signer = None
    for vin in tx.get("vin") or []:
        if not isinstance(vin, dict):
            continue
        prev = vin.get("prevout") or {}
        addr = prev.get("scriptpubkey_address")
        if not valid_btc_address(addr, network):
            continue
        if verify_btc_message(addr, payer_sig, msg, network):
            signer = addr
            break
    if signer is None:
        return _clean_fail("payerSig does not verify against any tx input "
                           "address (bind binding_message(txHash, resource) "
                           "with the paying address)",
                           replay_key=key)

    return True, {"paid_sats": paid_sats, "tx": h, "network": network,
                  "signer": signer, "confirmations": confirmations,
                  "replay_key": key}
