"""awLPay structured logging: JSON lines to stdout.

Every request the ASGI app serves emits exactly one JSON log line:

    {"ts": ..., "level": "info", "event": "request",
     "request_id": ..., "method": "POST", "path": "/api/pay/quote",
     "status": 200, "latency_ms": 3.41, "tier": "pro",
     "price_cents": 0, "mode": "mock-local"}

Fields:
    request_id  X-Request-Id passthrough, or a fresh uuid4 hex id.
                Echoed back to the caller as the X-Request-Id header.
    latency_ms  wall time from first byte in to response start.
    tier        requested fee tier ("free"/"pro"/"l33t") when the body
                parsed; null otherwise (404s, bad bodies, 402s).
    price_cents route price of the endpoint (0 = free quote, 25 = execute);
                null when the request never reached a priced route.
    mode        AWL_MODE, so logs say which deployment emitted them.

Nothing sensitive is logged: no request bodies, no headers, no keys.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timezone

MODE = os.environ.get("AWL_MODE", "mock-local")


def log_event(event: str, **fields) -> None:
    """Emit one JSON line to stdout. Never raises — logging must not
    break request handling."""
    try:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": "info",
            "event": event,
            **fields,
        }
        print(json.dumps(record), flush=True)
    except Exception:
        pass


class RequestLogMiddleware:
    """Pure-ASGI middleware: assigns/echoes X-Request-Id, times the
    request, and emits the single structured log line. Tier/price come
    from request.state, set by the endpoint handlers."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(
            (k.decode("latin-1").lower(), v.decode("latin-1"))
            for k, v in scope.get("headers", [])
        )
        request_id = headers.get("x-request-id") or uuid.uuid4().hex
        start = time.perf_counter()
        status_holder: dict = {}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                hdrs = list(message.get("headers", []))
                hdrs.append((b"x-request-id", request_id.encode("latin-1")))
                message = {**message, "headers": hdrs}
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            state = scope.get("state", {}) or {}
            latency_ms = round((time.perf_counter() - start) * 1000, 3)
            log_event(
                "request",
                request_id=request_id,
                method=scope.get("method"),
                path=scope.get("path"),
                status=status_holder.get("status"),
                latency_ms=latency_ms,
                tier=state.get("tier"),
                price_cents=state.get("price_cents"),
                mode=MODE,
            )


class PathNormalizeMiddleware:
    """Pure-ASGI middleware: strip a trailing slash (except root) before
    routing. Preserves the stdlib server's `path.rstrip('/')` behavior
    so /api/pay/quote/ keeps working."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            path = scope.get("path") or ""
            if len(path) > 1 and path.endswith("/"):
                scope = {**scope, "path": path.rstrip("/")}
        await self.app(scope, receive, send)
