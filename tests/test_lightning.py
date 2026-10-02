"""awLPay Lightning rail tests: preimage-proof verifier + mocked ZBD issuance.

Covers (fully offline — no network, no Lightning wallet):
  (a) config: lightning_enabled on/off (env read at call time)
  (b) pure helpers: preimage hex validation, strict integer millisats
      (floats rejected — the x402 ecosystem's Lightning unit bug)
  (c) decode_invoice: real locally-minted BOLT11 invoice -> payment_hash,
      amount_msats, expiry; malformed inputs refused
  (d) create_invoice: missing API key -> clean (None, None, None);
      mocked ZBD HTTP -> parses both response shapes, decodes the
      invoice authoritatively, refuses amount mismatch; request body
      asserted (amount as STRING msats, expiresIn NUMBER seconds,
      apikey header)
  (e) verify_lightning_payment: happy path, then each refusal — wrong
      preimage, short preimage, non-hex preimage, malformed
      payment_hash, underpaid invoice, expired invoice, replay;
      used_set never mutated (caller consumes the key)

No mainnet, no real funds, no ZBD calls. A testnet E2E would need a
ZBD API key plus a Lightning testnet wallet — see README/docs; not
attempted here.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

from server import lightning as ln  # noqa: E402

# ---------------------------------------------------------------- fixtures

PREIMAGE = secrets.token_bytes(32)
PREIMAGE_HEX = PREIMAGE.hex()
PAYMENT_HASH = hashlib.sha256(PREIMAGE).hexdigest()
MIN_MSATS = 5000


def _mint_invoice(amount_msats=MIN_MSATS, expire_secs=600):
    """Mint a real, validly-signed BOLT11 invoice with the bolt11 lib
    (local throwaway keys; never a real wallet)."""
    import bolt11
    from bolt11 import TagChar
    tags = bolt11.Tags()
    tags.add(TagChar.payment_hash, PAYMENT_HASH)
    tags.add(TagChar.payment_secret, secrets.token_hex(32))
    tags.add(TagChar.description, "awlpay lightning test invoice")
    tags.add(TagChar.expire_time, expire_secs)
    inv = bolt11.Bolt11(currency="bc", date=int(time.time()),
                        tags=tags, amount_msat=bolt11.MilliSatoshi(
                            amount_msats))
    return bolt11.encode(inv, private_key=secrets.token_hex(32))


BOLT11 = _mint_invoice()
EXPIRES_AT = int(time.time()) + 600


def _env(key, value):
    class Ctx:
        def __enter__(self):
            self.old = os.environ.get(key)
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
            return self

        def __exit__(self, *a):
            if self.old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = self.old
    return Ctx()


# ---------------------------------------------------------------- config

def test_enabled_when_key_set():
    with _env("AWL_ZBD_API_KEY", "zbd-project-key"):
        assert ln.lightning_enabled()


def test_disabled_without_key():
    with _env("AWL_ZBD_API_KEY", None):
        assert not ln.lightning_enabled()


def test_disabled_on_blank_key():
    with _env("AWL_ZBD_API_KEY", "   "):
        assert not ln.lightning_enabled()


def test_api_base_default_and_override():
    with _env("AWL_ZBD_API_BASE", None):
        assert ln.zbd_api_base() == "https://api.zebedee.io/v0"
    with _env("AWL_ZBD_API_BASE", "https://stage.example/v0/"):
        assert ln.zbd_api_base() == "https://stage.example/v0"


# ---------------------------------------------------------------- pure helpers

def test_preimage_hex_validation():
    assert ln.valid_preimage_hex(PREIMAGE_HEX)
    assert not ln.valid_preimage_hex(PREIMAGE_HEX[:-2])      # short
    assert not ln.valid_preimage_hex("z" * 64)               # non-hex
    assert not ln.valid_preimage_hex("0x" + PREIMAGE_HEX)    # 0x prefix
    assert not ln.valid_preimage_hex(PREIMAGE_HEX.upper())   # uppercase
    assert not ln.valid_preimage_hex(None)


def test_msats_strict_int():
    assert ln.msats_to_int(5000) == 5000
    assert ln.msats_to_int("5000") == 5000
    with pytest.raises(ValueError):
        ln.msats_to_int("50.5")      # floats rejected
    with pytest.raises(ValueError):
        ln.msats_to_int(50.5)
    with pytest.raises(ValueError):
        ln.msats_to_int("-1")
    with pytest.raises(ValueError):
        ln.msats_to_int(True)
    with pytest.raises(ValueError):
        ln.msats_to_int("abc")


def test_clamp_expires():
    assert ln.clamp_expires(900) == 900
    assert ln.clamp_expires(30) == 60
    assert ln.clamp_expires(99999) == 3600


# ---------------------------------------------------------------- decode

def test_decode_invoice_happy():
    d = ln.decode_invoice(BOLT11)
    assert d["payment_hash"] == PAYMENT_HASH
    assert d["amount_msats"] == MIN_MSATS
    assert d["is_mainnet"] is True
    assert d["currency"] == "bc"
    assert d["expires_at"] > time.time()
    assert d["description"] == "awlpay lightning test invoice"


def test_decode_invoice_rejects_junk():
    for bad in ("not an invoice", "", None, "lnbc50n1" + "z" * 60,
                "bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh"):
        with pytest.raises(ValueError):
            ln.decode_invoice(bad)


# ---------------------------------------------------------------- create_invoice (mocked ZBD)

def _mock_http(response):
    def fn(method, url, body, headers):
        fn.last = (method, url, body, headers)
        return response
    fn.last = None
    return fn


def _new_shape_response(bolt11_str, amount=MIN_MSATS):
    return {"success": True, "message": "Successfully created Charge.",
            "data": {"unit": "msats", "amount": str(amount),
                     "status": "pending",
                     "invoice": {"request": bolt11_str,
                                 "expiresAt": "2030-01-01T00:00:00.000Z"}}}


def _old_shape_response(bolt11_str, amount=MIN_MSATS):
    return {"success": True,
            "data": {"unit": "msats", "amount": str(amount),
                     "status": "CHARGE_PENDING",
                     "invoiceRequest": bolt11_str,
                     "invoiceExpiresAt": "2030-01-01T00:00:00.000Z"}}


def test_create_invoice_no_key_is_clean_failure():
    with _env("AWL_ZBD_API_KEY", None):
        assert ln.create_invoice(MIN_MSATS, "x402 challenge") == (
            None, None, None)


def test_create_invoice_new_shape():
    mock = _mock_http(_new_shape_response(BOLT11))
    ln.set_test_http(mock)
    with _env("AWL_ZBD_API_KEY", "zbd-project-key"):
        bolt11_str, ph, expires_at = ln.create_invoice(MIN_MSATS,
                                                       "x402 challenge")
    ln.clear_test_http()
    assert bolt11_str == BOLT11
    assert ph == PAYMENT_HASH
    assert expires_at > time.time()
    method, url, body, headers = mock.last
    assert method == "POST"
    assert url == "https://api.zebedee.io/v0/charges"
    assert body == {"amount": str(MIN_MSATS),      # STRING msats
                    "description": "x402 challenge",
                    "expiresIn": 900}              # NUMBER seconds
    assert headers["apikey"] == "zbd-project-key"  # apikey header, not Auth


def test_create_invoice_old_shape():
    mock = _mock_http(_old_shape_response(BOLT11))
    ln.set_test_http(mock)
    with _env("AWL_ZBD_API_KEY", "k"):
        bolt11_str, ph, _ = ln.create_invoice(MIN_MSATS, "x402")
    ln.clear_test_http()
    assert bolt11_str == BOLT11 and ph == PAYMENT_HASH


def test_create_invoice_zbd_error_is_clean():
    mock = _mock_http({"success": False, "message": "bad key"})
    ln.set_test_http(mock)
    with _env("AWL_ZBD_API_KEY", "bad-key"):
        assert ln.create_invoice(MIN_MSATS, "x402") == (None, None, None)
    ln.clear_test_http()


def test_create_invoice_amount_mismatch_refused():
    # ZBD minted a different amount than asked -> refuse.
    mock = _mock_http(_new_shape_response(_mint_invoice(MIN_MSATS + 1),
                                          amount=MIN_MSATS + 1))
    ln.set_test_http(mock)
    with _env("AWL_ZBD_API_KEY", "k"):
        assert ln.create_invoice(MIN_MSATS, "x402") == (None, None, None)
    ln.clear_test_http()


def test_create_invoice_bad_amount_is_clean():
    with _env("AWL_ZBD_API_KEY", "k"):
        assert ln.create_invoice(0, "x402") == (None, None, None)
        assert ln.create_invoice("not-a-number", "x402") == (None, None, None)
        assert ln.create_invoice(MIN_MSATS, "") == (None, None, None)


def test_create_invoice_http_failure_is_clean():
    def boom(method, url, body, headers):
        raise ConnectionError("dns exploded")
    ln.set_test_http(boom)
    with _env("AWL_ZBD_API_KEY", "k"):
        assert ln.create_invoice(MIN_MSATS, "x402") == (None, None, None)
    ln.clear_test_http()


# ---------------------------------------------------------------- verifier

def _verify(**kw):
    args = dict(preimage_hex=PREIMAGE_HEX, payment_hash_hex=PAYMENT_HASH,
                amount_msats=MIN_MSATS, min_msats=MIN_MSATS,
                expires_at=EXPIRES_AT, used_set=set())
    args.update(kw)
    return ln.verify_lightning_payment(**args)


def test_verify_happy_path():
    used = set()
    ok, info = _verify(used_set=used)
    assert ok, info
    assert info["amount_msats"] == MIN_MSATS
    assert info["payment_hash"] == PAYMENT_HASH
    assert info["replay_key"] == "lightning:" + PAYMENT_HASH
    # verifier never consumes the replay key itself
    assert used == set()


def test_verify_wrong_preimage():
    wrong = hashlib.sha256(b"something else").hexdigest()
    ok, info = _verify(preimage_hex=wrong)
    assert not ok and "does not match" in info["reason"]


def test_verify_short_preimage():
    ok, info = _verify(preimage_hex=PREIMAGE_HEX[:-2])
    assert not ok and "preimage format" in info["reason"]


def test_verify_nonhex_preimage():
    ok, info = _verify(preimage_hex="z" * 64)
    assert not ok and "preimage format" in info["reason"]


def test_verify_uppercase_preimage_accepted():
    ok, info = _verify(preimage_hex=PREIMAGE_HEX.upper())
    assert ok, info


def test_verify_bad_payment_hash_format():
    ok, info = _verify(payment_hash_hex="zzz")
    assert not ok and "payment_hash" in info["reason"]


def test_verify_underpaid():
    ok, info = _verify(amount_msats=MIN_MSATS - 1)
    assert not ok and "underpaid" in info["reason"]
    assert info["amount_msats"] == MIN_MSATS - 1


def test_verify_overpaid_ok():
    ok, info = _verify(amount_msats=MIN_MSATS + 100)
    assert ok, info


def test_verify_expired():
    ok, info = _verify(expires_at=int(time.time()) - 1)
    assert not ok and "expired" in info["reason"]


def test_verify_replay():
    used = {"lightning:" + PAYMENT_HASH}
    ok, info = _verify(used_set=used)
    assert not ok and info["reason"].startswith("replay:")
    assert info["replay_key"] == "lightning:" + PAYMENT_HASH


def test_verify_replay_key_scoped_to_hash():
    used = {"lightning:" + "0" * 64}
    ok, info = _verify(used_set=used)
    assert ok, info  # different hash -> different key -> not a replay


def test_verify_rejects_float_amounts():
    ok, info = _verify(amount_msats=50.5)
    assert not ok and "bad amount_msats" in info["reason"]
