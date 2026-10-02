#!/usr/bin/env python3
"""Create the awlpay Fly machine WITHOUT regenerating the relayer seed.

The AWL_RELAYER_KEY secret was already stored (2026-09-28); this only
creates the machine with the small self-provisioning boot script.
"""
import json
import sys

sys.path.insert(0, "/home/hatch/workspace/skills/fly/bin")
from flym import api as m_api  # Machines API

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


def main():
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
    mid = out.get("id")
    print("machine:", mid, out.get("state"))
    open("/home/hatch/.awlpay_mid", "w").write(mid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
