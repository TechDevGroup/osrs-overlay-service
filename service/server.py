"""Asyncio TCP server — the overlay bridge service (see PROTOCOL.md).

The plugin is a dumb TCP client; THIS is the server holding all logic. One
connection at a time is fine. Newline-delimited JSON on 127.0.0.1:43594.

Lifecycle: hello -> subscribe; each `state` -> `render` (echo seq); `event` ->
action log + session accounting; `discovered` -> persist bank/widget bounds.

Hot reload: policy_bf.py mtime is watched; on change the module is reimported
(importlib.reload) WITHOUT dropping the socket, so game logic iterates without
restarting the RuneLite client. Never crash on a bad line — log and continue.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

from . import policy_bf
from .state_store import StateStore

log = logging.getLogger("overlay-service")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 43594
TTL_TICKS = 2


class OverlayService:
    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
                 data_dir: Optional[Path] = None,
                 bar_type: str = "AUTO", coffer_low_minutes: int = 20,
                 coffer_critical_gp: int = 0):
        self.host = host
        self.port = port
        self.store = StateStore(data_dir)
        self.bar_type = bar_type
        self.coffer_low_minutes = coffer_low_minutes
        self.coffer_critical_gp = coffer_critical_gp

        self.policy = policy_bf
        self._policy_path = Path(policy_bf.__file__)
        self._policy_mtime = self._policy_path.stat().st_mtime
        self._last_snapshot = None  # for rebuild-on-reconnect

    # ── hot reload ────────────────────────────────────────────────────────────
    def _maybe_reload_policy(self) -> None:
        try:
            m = self._policy_path.stat().st_mtime
        except OSError:
            return
        if m != self._policy_mtime:
            self._policy_mtime = m
            try:
                self.policy = importlib.reload(self.policy)
                log.info("policy reloaded")
            except Exception as e:  # keep serving the old module on a bad edit
                log.error("policy reload failed, keeping previous: %s", e)

    # ── message handlers ──────────────────────────────────────────────────────
    def _handle_state(self, msg: Dict[str, Any]) -> Dict[str, Any]:
        ctx = self.store.ctx(self.bar_type, self.coffer_low_minutes, self.coffer_critical_gp)
        snap = self.policy.build_snapshot(msg, ctx)
        # rolling XP/hr sampler (needs wall clock + bar type -> done here, not in the pure policy)
        self.store.record_sample(snap.bars_collected)
        xp_per_bar = snap.bar_type.xp_per_bar if snap.bar_type else 0.0
        snap = snap.replace(rolling_xp_line=self.store.rolling_xp_line(xp_per_bar))
        self._last_snapshot = snap
        guidance = self.policy.derive(snap)
        directives = self.policy.build_directives(snap, guidance, self.store.layout_for_policy())
        return {"t": "render", "seq": msg.get("seq", 0),
                "ttlTicks": TTL_TICKS, "directives": directives}

    def _handle_event(self, msg: Dict[str, Any]) -> None:
        self.store.log_action(msg)

    def _handle_discovered(self, discovered: Dict[str, Any]) -> None:
        self.store.record_discovered(discovered)

    def handle_message(self, msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Process one decoded message; return a reply dict or None."""
        t = msg.get("t")
        if t == "hello":
            log.info("client hello: %s", msg.get("client"))
            return dict(self.policy.SUBSCRIBE)
        if t == "state":
            # `discovered` may piggyback on state messages.
            if msg.get("discovered"):
                self._handle_discovered(msg["discovered"])
            self._maybe_reload_policy()
            return self._handle_state(msg)
        if t == "event":
            self._handle_event(msg)
            return None
        if t == "discovered":
            self._handle_discovered(msg.get("discovered") or msg)
            return None
        if t == "pong":
            return None
        log.warning("unknown message type: %r", t)
        return None

    # ── connection loop ───────────────────────────────────────────────────────
    async def _serve_client(self, reader: asyncio.StreamReader,
                            writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        log.info("client connected: %s", peer)
        # Rebuild from persisted snapshot: session counters/hotspots/layout are
        # already loaded in the store; nothing to send until the client says hello.
        try:
            while True:
                try:
                    line = await reader.readline()
                except (ValueError, asyncio.LimitOverrunError):
                    # oversized line despite the raised limit — drop this connection
                    # so the client reconnects cleanly rather than wedging.
                    log.warning("oversized line — dropping connection to force reconnect")
                    break
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    log.warning("bad JSON line (%d bytes) — skipping", len(line))
                    continue
                try:
                    reply = self.handle_message(msg)
                except Exception as e:  # never crash on a single bad message
                    log.exception("handler error: %s", e)
                    continue
                if reply is not None:
                    writer.write((json.dumps(reply, separators=(",", ":")) + "\n").encode("utf-8"))
                    await writer.drain()
        except (ConnectionResetError, asyncio.IncompleteReadError):
            pass
        finally:
            log.info("client disconnected: %s", peer)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    # Bank-open state messages (full bank contents + ~60 discovered item bounds)
    # exceed asyncio's default 64 KiB readline limit, which crashes the read loop
    # exactly when the user opens the bank. Raise it generously.
    READ_LIMIT = 16 * 1024 * 1024

    async def run(self) -> None:
        server = await asyncio.start_server(
            self._serve_client, self.host, self.port, limit=self.READ_LIMIT)
        addrs = ", ".join(str(s.getsockname()) for s in server.sockets)
        log.info("overlay service listening on %s (data dir: %s)", addrs, self.store.dir)
        async with server:
            await server.serve_forever()


def build_from_env() -> OverlayService:
    host = os.environ.get("OVERLAY_HOST", DEFAULT_HOST)
    port = int(os.environ.get("OVERLAY_PORT", DEFAULT_PORT))
    data_dir = os.environ.get("OVERLAY_DATA_DIR")
    bar_type = os.environ.get("OVERLAY_BAR_TYPE", "AUTO")
    return OverlayService(
        host=host, port=port,
        data_dir=Path(data_dir) if data_dir else None,
        bar_type=bar_type,
    )


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("OVERLAY_LOG", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    service = build_from_env()
    try:
        asyncio.run(service.run())
    except KeyboardInterrupt:
        log.info("shutting down")
