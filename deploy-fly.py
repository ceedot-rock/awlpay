#!/usr/bin/env python3
"""awLPay v1 Fly deploy via Machines API (no flyctl auth on this VM).

Steps:
  1. Generate a fresh Ed25519 relayer seed (64 hex) IN-PROCESS — never printed.
  2. Set it as the AWL_RELAYER_KEY Fly secret via GraphQL setSecrets.
  3. Create the machine (python:3.12-slim + self-provisioning boot script).

Only status is printed. The secret value never leaves this process.
"""
import json
import secrets
import sys

sys.path.insert(0, "/home/hatch/workspace/skills/fly/bin")
from flyapi import api as fly_api  # api.fly.io (GraphQL + REST)
from flym import api as m_api       # Machines API

APP = "awlpay"

BOOT = """set -e
cd /app
# wait for code to arrive via `put`
while [ ! -f /app/server/requirements.txt ]; do sleep 5; done
# install deps once; /app persists across restarts
if ! command -v uvicorn >/dev/null 2>&1; then
  pip install --no-cache-dir -r /app/server/requirements.txt
fi
touch /app/.boot-done
exec uvicorn server.app:app --host 0.0.0.0 --port 8080 --no-access-log --log-level warning
"""


def set_secret(key, value):
    query = (
        "mutation($input: SetSecretsInput!) {"
        " setSecrets(input: $input) { release { id version } } }"
    )
    payload = {
        "query": query,
        "variables": {
            "input": {
                "appId": APP,
                "secrets": [{"key": key, "value": value}],
            }
        },
    }
    st, out = fly_api("POST", "/graphql", payload)
    return st, out


def main():
    # 1. fresh relayer seed — generated here, never printed or logged
    seed_hex = secrets.token_hex(32)
    assert len(seed_hex) == 64

    # 2. store as Fly secret
    st, out = set_secret("AWL_RELAYER_KEY", seed_hex)
    ok = st == 200 and isinstance(out, dict) and "errors" not in out
    print("setSecrets:", "OK" if ok else "FAILED", f"(HTTP {st})")
    if not ok:
        print(json.dumps(out)[:1500])
        return 1

    # 3. create the machine
    machine_cfg = {
        "name": "api-1",
        "region": "ewr",
        "config": {
            "image": "python:3.12-slim",
            "guest": {"cpu_kind": "shared", "cpus": 1, "memory_mb": 256},
            "env": {
                "PORT": "8080",
                "PYTHONUNBUFFERED": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
                "AWL_MODE": "prod",
                "AWL_ORACLE": "coingecko",
                "AWL_TEST_MODE": "0",
            },
            "init": {"cmd": ["sh", "-c", BOOT]},
            "restart": {"policy": "always"},
            "services": [
                {
                    "protocol": "tcp",
                    "internal_port": 8080,
                    "autostart": True,
                    "ports": [
                        {"port": 80, "handlers": ["http"]},
                        {"port": 443, "handlers": ["http", "tls"]},
                    ],
                }
            ],
            "auto_destroy": False,
        },
    }
    st, out = m_api("POST", f"/v1/apps/{APP}/machines", machine_cfg, timeout=300)
    if not (200 <= st < 300):
        print("machine-create FAILED", f"(HTTP {st})")
        print(json.dumps(out)[:2000] if isinstance(out, dict) else str(out)[:2000])
        return 1
    print("machine:", out.get("id"), out.get("state"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
