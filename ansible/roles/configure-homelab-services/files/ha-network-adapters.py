"""Set Home Assistant's configured network adapters (core.network) by name.

Run inside the homeassistant container (it has aiohttp), script on stdin:

    docker exec -i -e HA_TOKEN homeassistant python3 - '["int0","iot0"]' < this

Waits for HA's websocket API (with backoff, it may be starting after a
recreate), reads the current adapters, and sends network/configure only when
configured_adapters differs from the wanted list. Every wanted name must be a
live adapter with an IPv4 address. Prints "unchanged" or "changed: OLD -> NEW".
The token comes from the environment and is never printed.
"""

import asyncio
import json
import os
import sys
import time

import aiohttp

URL = "ws://127.0.0.1:8123/api/websocket"
DEADLINE = 600  # seconds to wait for HA to come up


async def call(ws, msg_id, payload):
    await ws.send_json({"id": msg_id, **payload})
    while True:
        msg = await ws.receive_json(timeout=30)
        if msg.get("id") == msg_id:
            if not msg.get("success"):
                raise RuntimeError(f"{payload['type']} failed: {msg.get('error')}")
            return msg.get("result")


async def attempt(token, want):
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect(URL, timeout=aiohttp.ClientWSTimeout(ws_close=10)) as ws:
            hello = await ws.receive_json(timeout=30)
            if hello.get("type") != "auth_required":
                raise RuntimeError(f"unexpected greeting {hello.get('type')}")
            await ws.send_json({"type": "auth", "access_token": token})
            auth = await ws.receive_json(timeout=30)
            if auth.get("type") != "auth_ok":
                raise PermissionError("HA rejected the token")
            net = await call(ws, 1, {"type": "network"})
            live = {a["name"]: a for a in net.get("adapters") or []}
            missing = [n for n in want if n not in live or not live[n].get("ipv4")]
            if missing:
                raise SystemExit(f"adapters {missing} not present with IPv4 (have {sorted(live)})")
            cur = net.get("configured_adapters") or []
            if sorted(cur) == sorted(want):
                return "unchanged"
            await call(ws, 2, {"type": "network/configure", "config": {"configured_adapters": want}})
            return f"changed: {cur} -> {want}"


async def main():
    token = os.environ.get("HA_TOKEN") or sys.exit("HA_TOKEN not set")
    want = json.loads(sys.argv[1])
    if not want or not all(isinstance(n, str) and n for n in want):
        sys.exit("want a non-empty JSON list of adapter names")
    start, delay = time.monotonic(), 2
    while True:
        try:
            print(await attempt(token, want))
            return
        except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionError, TypeError) as e:
            # TypeError: receive_json on a closed socket while HA is starting
            if time.monotonic() - start > DEADLINE:
                sys.exit(f"HA websocket not reachable after {DEADLINE}s: {type(e).__name__}")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)


asyncio.run(main())
