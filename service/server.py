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
import collections
import importlib
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

from . import policy_bf, action_model
from .state_store import StateStore

log = logging.getLogger("overlay-service")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 43594
TTL_TICKS = 5           # bridge keeps highlights this many ticks without a fresh
                        # render — rides over brief service/network gaps (anti-flicker)


def _dir_key(d):
    """Stable identity of a highlight target, so the same slot/object across ticks
    is recognised as 'the same indicator' (regardless of color/kind changes)."""
    k = d.get("kind")
    if k in ("bankItem", "bankItemPredicted"):
        return ("bank", d.get("id"))
    if k == "invItem":
        return ("inv", d.get("id"))
    if k == "object":
        return ("obj", d.get("id"))
    if k in ("tile", "worldArrow"):
        return (k, d.get("x"), d.get("y"))
    if k in ("widget", "widgetPredicted"):
        return ("close", d.get("group"), d.get("child"))
    if k == "text":
        return ("hud", d.get("anchor"))
    return (k, d.get("x"), d.get("y"), d.get("id"))


class DirectiveSmoother:
    """Anti-flicker persistence. Treats a withdrawal/nav step as a continuous PHASE:
    once a target's indicator is shown it is held for a short grace window, so a
    few jittery ticks (state briefly changing the derived target) don't blink it
    off and back on. A target that stays absent past the grace expires normally.
    Also debounces rapid color/kind churn on a still-present target."""
    GRACE = 4            # hold a vanished target this many ticks
    COLOR_HOLD = 2       # don't restyle a present target more often than this

    def __init__(self):
        self.cache = {}  # key -> {"dir": d, "seen": tick, "styled": tick}

    def reset(self):
        self.cache.clear()

    def smooth(self, directives, tick):
        fresh = {}
        for d in directives:
            fresh[_dir_key(d)] = d
        out = []
        for key, d in fresh.items():
            ent = self.cache.get(key)
            if ent is not None and d != ent["dir"] and (tick - ent["styled"]) < self.COLOR_HOLD:
                # target still present but style changed too soon -> keep prior look
                out.append(ent["dir"])
                ent["seen"] = tick
            else:
                styled = tick if (ent is None or d != ent["dir"]) else ent["styled"]
                self.cache[key] = {"dir": d, "seen": tick, "styled": styled}
                out.append(d)
        # persist recently-vanished targets through the grace window (no blink-off)
        for key, ent in list(self.cache.items()):
            if key in fresh:
                continue
            if key[0] == "hud" or tick - ent["seen"] > self.GRACE:
                del self.cache[key]        # HUD never persists stale; others expire
            else:
                out.append(ent["dir"])
        return out


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
        # learned next-action model, built from the user's own action log
        self.model = action_model.ActionModel(self.store.actions_path)
        self.history: "collections.deque[str]" = collections.deque(maxlen=2)
        self.smoother = DirectiveSmoother()
        self._tick = 0
        log.info("action model built from log: %d transitions observed", self.model.count)

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
        # remember the detected bar type so it sticks across empty-inventory ticks
        if snap.bar_type is not None:
            self.store.last_bar_type = snap.bar_type.name
        self._last_snapshot = snap
        guidance = self.policy.derive(snap)
        # Look-ahead plan from the learned model: an ordered list of the next few
        # actions (next-first), each mapped to a concrete target. This LEADS the
        # player — primary = plan[0], on-deck = plan[1] — so highlights appear a
        # step before the action, not after it. Unmappable tokens become None and
        # are skipped downstream.
        plan_tokens = self.model.plan(tuple(self.history), steps=3)
        plan_targets = [action_model.token_target(t, snap.bar_type) for t in plan_tokens]
        directives = self.policy.build_directives(
            snap, guidance, self.store.layout_for_policy(), plan=plan_targets)
        # anti-flicker: hold indicators steadily across the phase (see DirectiveSmoother)
        self._tick += 1
        directives = self.smoother.smooth(directives, self._tick)
        return {"t": "render", "seq": msg.get("seq", 0),
                "ttlTicks": TTL_TICKS, "directives": directives}

    def _handle_event(self, msg: Dict[str, Any]) -> None:
        self.store.log_action(msg)
        # feed the learned model live so it adapts to the current session
        tok = action_model.canonical_token(msg)
        if tok:
            self.model.observe(tok)
            self.history.append(tok)

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
        self.smoother.reset()   # fresh anti-flicker state per connection
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
