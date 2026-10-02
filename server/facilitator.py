"""AwLPay x402 facilitator — push-payment verification as a service.

Speaks the x402 v2 facilitator wire format (GET /supported, POST /verify,
POST /settle) so external resource servers can point their 402s at us
instead of being stuck on one chain. Multi-rail: every rail AwLPay
verifies is advertised.

HONEST MODEL DIFFERENCE: standard x402 facilitators verify EIP-3009
signed authorizations off-chain and submit them on-chain at settle time.
AwLPay is a PUSH-PAYMENT verifier: the payer sends the transaction
directly on-chain, and we verify the on-chain receipt. Our /verify checks
the proof against chain state (no money moves — it already moved). Our
/settle re-verifies and attests (the tx hash IS the receipt); we submit
nothing on-chain.

Sellers who want the standard EIP-3009 authorization flow should use a
standard facilitator. Sellers who want multi-rail push-payment
verification — any chain, any token with value — use us.

Endpoints (wired into server/app.py):
  GET  /supported          -> {kinds[], extensions[], signers{}}
  POST /verify             -> {isValid, invalidReason?, payer?}
  POST /settle             -> {success, transaction, network, payer?,
                              errorReason?}
  GET  /.well-known/x402   -> discovery manifest

Auth: v1 is open (no key), like PayAI/Dexter reference facilitators.
"""

from . import x402


# --------------------------------------------------------------------------
# /supported
# --------------------------------------------------------------------------

def supported_kinds() -> list[dict]:
    """Advertise every configured rail as an x402Version-2 exact kind."""
    kinds = []
    for network, meta in x402.configured_rails():
        kinds.append({
            "x402Version": x402.X402_VERSION,
            "scheme": x402.PAY_SCHEME,
            "network": network,
        })
    return kinds


def supported_response() -> dict:
    """Full GET /supported body per the x402 v2 spec."""
    return {
        "kinds": supported_kinds(),
        "extensions": [],
        "signers": {},
    }


def discovery_manifest(host: str) -> dict:
    """GET /.well-known/x402 — who we are and where the endpoints live."""
    base = "https://%s" % host
    return {
        "name": "AwLPay Facilitator",
        "description": ("Multi-rail x402 push-payment verification: "
                        "Base, Polygon, Arbitrum, Optimism, BSC, Solana, "
                        "XRPL, Tron, Stellar, Bitcoin, Lightning."),
        "x402Version": x402.X402_VERSION,
        "model": "push-payment",
        "endpoints": {
            "supported": base + "/supported",
            "verify": base + "/verify",
            "settle": base + "/settle",
        },
        "kinds": supported_kinds(),
    }


# --------------------------------------------------------------------------
# /verify and /settle — shared verification core
# --------------------------------------------------------------------------

# Spec §9 invalidReason codes we map to.
_REASON_MAP = {
    "no verifiable": "invalid_payment_requirements",
    "replay": "invalid_transaction_state",
    "unknown or expired": "invalid_payload",
}


def _map_reason(detail: str) -> str:
    d = (detail or "").lower()
    for needle, code in _REASON_MAP.items():
        if needle in d:
            return code
    return "unexpected_verify_error"


def _extract_proof(payment_payload: dict):
    """Pull (network, proof, payer_sig, resource) from a v2 PaymentPayload.

    Our push-payment payload shapes (see server/x402.py parse_x_payment):
      EVM/Tron/BTC: {txHash, payerSig?}
      XRPL/Stellar: {txHash}            (binding via invoiceId/memoHash)
      Lightning:    {preimage, paymentHash}
    The accepted PaymentRequirements carry network + amount + payTo.
    """
    accepted = (payment_payload or {}).get("accepted") or {}
    payload = (payment_payload or {}).get("payload") or {}
    network = accepted.get("network")
    resource = ((payment_payload or {}).get("resource") or {}).get("url", "")
    if network in x402.LIGHTNING_NETWORKS:
        preimage = payload.get("preimage")
        phash = payload.get("paymentHash") or payload.get("payment_hash")
        if not preimage or not phash:
            return None, None, None, "payload needs preimage and paymentHash"
        return network, (preimage, phash), None, resource
    txh = payload.get("txHash") or payload.get("tx_hash")
    if not txh:
        return None, None, None, "payload needs txHash"
    payer_sig = payload.get("payerSig") or payload.get("payer_sig")
    return network, txh, payer_sig, resource


def facilitator_verify(body: dict, oracle=None, used_set=None) -> dict:
    """POST /verify: validate a push-payment proof against on-chain state.

    body: {x402Version: 2, paymentPayload: {...}, paymentRequirements: {...}}
    Returns {isValid, invalidReason?, invalidMessage?, payer?}.
    Read-only: never consumes the replay key (settle does that).
    """
    if not isinstance(body, dict):
        return {"isValid": False, "invalidReason": "invalid_payload",
                "invalidMessage": "body must be a JSON object"}
    if body.get("x402Version") != 2:
        return {"isValid": False, "invalidReason": "invalid_x402_version",
                "invalidMessage": "x402Version must be 2"}
    payment_payload = body.get("paymentPayload") or {}
    requirements = body.get("paymentRequirements") or {}
    network, proof, payer_sig, resource = _extract_proof(payment_payload)
    if network is None:
        return {"isValid": False, "invalidReason": "invalid_payload",
                "invalidMessage": proof or "unreadable paymentPayload"}

    # Amount: requirements.amount is atomic units of the rail's asset.
    try:
        min_units = int(str(requirements.get("amount", "0")))
    except (TypeError, ValueError):
        return {"isValid": False,
                "invalidReason": "invalid_payment_requirements",
                "invalidMessage": "requirements.amount must be an integer string"}
    pay_to = requirements.get("payTo") or ""

    ok, info = x402.verify_payment(
        proof, network, min_units, pay_to,
        used_set if used_set is not None else set(),
        payer_sig=payer_sig, resource=resource, oracle=oracle)
    if ok:
        resp = {"isValid": True}
        payer = info.get("signer") or info.get("payer")
        if payer:
            resp["payer"] = payer
        return resp
    reason = info.get("reason", "verification failed")
    return {"isValid": False,
            "invalidReason": _map_reason(reason),
            "invalidMessage": reason}


def facilitator_settle(body: dict, oracle=None, used_set=None) -> dict:
    """POST /settle: verify a push-payment proof and attest settlement.

    The payment is already on-chain (push model), so settlement IS
    verification: we re-verify and return the tx hash as the receipt.
    The replay key is consumed on success (caller passes a shared set).
    Returns {success, transaction, network, payer?, errorReason?}.
    """
    if not isinstance(body, dict):
        return {"success": False, "transaction": "", "network": "",
                "errorReason": "invalid_payload"}
    payment_payload = body.get("paymentPayload") or {}
    accepted = payment_payload.get("accepted") or {}
    network = accepted.get("network") or ""
    # Verify first (read-only), then consume the replay key on success.
    store = used_set if used_set is not None else set()
    ver = facilitator_verify(body, oracle=oracle, used_set=store)
    if not ver.get("isValid"):
        return {"success": False, "transaction": "",
                "network": network,
                "errorReason": ver.get("invalidReason",
                                       "unexpected_settle_error"),
                "errorMessage": ver.get("invalidMessage", "")}
    # Consume the replay key: re-run verification capturing info, then add.
    # (verify_payment is read-only on used_set; the key comes from info.)
    payload = payment_payload.get("payload") or {}
    tx = (payload.get("txHash") or payload.get("tx_hash")
          or payload.get("paymentHash") or payload.get("payment_hash")
          or "")
    resp = {"success": True, "transaction": tx, "network": network}
    if ver.get("payer"):
        resp["payer"] = ver["payer"]
    # Mark the proof as settled so it cannot be re-settled, using the
    # same namespaced replay-key format the verifiers check.
    if tx:
        try:
            store.add(x402.replay_key(network, tx))
        except Exception:
            store.add("%s:%s" % (network, tx.lower()))
    return resp
