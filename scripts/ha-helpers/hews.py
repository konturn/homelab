"""Tiny HA websocket client: python3 hews.py '<json message without id>' ...
Token from HASS_TOKEN env. Prints each result (success + result)."""
import asyncio
import json
import os
import sys

import aiohttp


async def main(msgs):
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect("http://127.0.0.1:8123/api/websocket") as ws:
            await ws.receive_json()
            await ws.send_json({"type": "auth", "access_token": os.environ["HASS_TOKEN"]})
            auth = await ws.receive_json()
            if auth.get("type") != "auth_ok":
                print("auth failed")
                return 1
            for i, m in enumerate(msgs, 1):
                msg = json.loads(m)
                msg["id"] = i
                await ws.send_json(msg)
                while True:
                    r = await ws.receive_json()
                    if r.get("id") == i and r.get("type") == "result":
                        break
                print(json.dumps({"ok": r.get("success"), "error": r.get("error"), "result": r.get("result")})[:1500])
    return 0


sys.exit(asyncio.run(main(sys.argv[1:])))
