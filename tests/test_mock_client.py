"""Integration test: a tiny mock plugin does hello -> subscribe -> one state ->
render over a real TCP socket against the asyncio server."""
import asyncio
import json

import pytest

from service import ids
from service.server import OverlayService


async def _roundtrip(tmp_path):
    service = OverlayService(host="127.0.0.1", port=0, data_dir=tmp_path)
    server = await asyncio.start_server(
        service._serve_client, service.host, service.port)
    port = server.sockets[0].getsockname()[1]

    async with server:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)

        async def send(obj):
            writer.write((json.dumps(obj) + "\n").encode())
            await writer.drain()

        async def recv():
            line = await asyncio.wait_for(reader.readline(), timeout=5)
            return json.loads(line)

        # 1. hello -> subscribe
        await send({"t": "hello", "proto": 1, "client": "mock", "ver": "0.0.1"})
        sub = await recv()

        # 2. state -> render
        state = {
            "t": "state", "seq": 7, "tick": 100,
            "player": {"x": 1948, "y": 4957, "plane": 0},
            "inv": [],
            "varbits": {str(ids.VAR_FURNACE_COAL): 2, str(ids.VAR_COFFER): 200000,
                       str(ids.VAR_FURNACE_ADAMANTITE_ORE): 0},
            "bank": {"open": True, "items": [{"id": ids.ITEM_COAL, "qty": 1000, "slot": 4}]},
            "objects": [],
        }
        # force adamantite via service config so an empty inv still detects a type
        service.bar_type = "ADAMANTITE"
        await send(state)
        render = await recv()

        writer.close()
        await writer.wait_closed()
        return sub, render


def test_hello_subscribe_state_render(tmp_path):
    sub, render = asyncio.run(_roundtrip(tmp_path))

    assert sub["t"] == "subscribe"
    assert "inventory" in sub["containers"] and "bank" in sub["containers"]
    assert ids.VAR_COFFER in sub["varbits"]
    assert ids.CONVEYOR_BELT in sub["objects"]
    assert sub["tickState"] is True

    assert render["t"] == "render"
    assert render["seq"] == 7          # echoes the state seq
    assert render["ttlTicks"] == 2
    assert isinstance(render["directives"], list) and render["directives"]
    kinds = [d["kind"] for d in render["directives"]]
    assert "bankItem" in kinds         # withdraw coal highlight
    assert "text" in kinds             # HUD


def test_bad_line_does_not_crash(tmp_path):
    async def run():
        service = OverlayService(host="127.0.0.1", port=0, data_dir=tmp_path)
        server = await asyncio.start_server(service._serve_client, service.host, service.port)
        port = server.sockets[0].getsockname()[1]
        async with server:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"not json at all\n")
            await writer.drain()
            # still responsive after a bad line
            writer.write((json.dumps({"t": "hello", "proto": 1}) + "\n").encode())
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), timeout=5)
            writer.close()
            await writer.wait_closed()
            return json.loads(line)

    reply = asyncio.run(run())
    assert reply["t"] == "subscribe"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
