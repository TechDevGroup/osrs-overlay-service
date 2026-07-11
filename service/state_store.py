"""Persistence + session accounting for the overlay service.

Files under the data dir (default ~/.runelite/overlay-service/, or ./data):
  hotspots.json     learned standing tiles per canonical object id
  bank-layout.json  discovered bank-item + widget canvas bounds (from the plugin's
                    `discovered` reports); re-issued as *Predicted directives when
                    the source is offscreen/closed
  actions.log       append-only log of every `event` message

Also tracks live session state that survives a plugin reconnect: coal-bag count,
cumulative coal/ore deposited + bars collected, and the session start time — so
the trip-computer HUD keeps counting across reconnects.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

from . import ids

# Seed hotspots (known standing tiles), keyed by canonical object id.
#   dispenser (1940,4962), approach/belt (1942,4967).
_SEED_HOTSPOTS = {
    str(ids.DISPENSER_BASE): {"x": 1940, "y": 4962, "plane": 0},
    str(ids.CONVEYOR_BELT): {"x": 1942, "y": 4967, "plane": 0},
    str(ids.BANK_CHEST): {"x": 1948, "y": 4957, "plane": 0},
}


def _button_like(box: Dict[str, Any]) -> bool:
    """A close button is a small, roughly-square button — not a tall/thin scrollbar
    or a big container. Filters out mis-matched 'Close' widgets."""
    w = box.get("w") or 0
    h = box.get("h") or 0
    if not (8 <= w <= 60 and 8 <= h <= 60):
        return False
    return max(w, h) <= 2.5 * min(w, h)


def _valid_close(box: Dict[str, Any], container: Optional[Dict[str, Any]]) -> bool:
    """A good close button: button-like AND near the bank container's top edge."""
    if not _button_like(box):
        return False
    top = (container or {}).get("y")
    return top is None or (box.get("y", 0) - top) <= 60


def default_data_dir() -> Path:
    rl = Path.home() / ".runelite" / "overlay-service"
    try:
        rl.mkdir(parents=True, exist_ok=True)
        return rl
    except OSError:
        d = Path.cwd() / "data"
        d.mkdir(parents=True, exist_ok=True)
        return d


class StateStore:
    def __init__(self, data_dir: Optional[Path] = None):
        self.dir = Path(data_dir) if data_dir else default_data_dir()
        self.dir.mkdir(parents=True, exist_ok=True)
        self.hotspots_path = self.dir / "hotspots.json"
        self.layout_path = self.dir / "bank-layout.json"
        self.actions_path = self.dir / "actions.log"

        self.hotspots: Dict[str, Any] = self._load(self.hotspots_path, dict(_SEED_HOTSPOTS))
        # merge seeds without clobbering learned ones
        for k, v in _SEED_HOTSPOTS.items():
            self.hotspots.setdefault(k, v)
        self.layout: Dict[str, Any] = self._load(self.layout_path, {"bankItems": {}, "widgets": {}})
        self.layout.setdefault("bankItems", {})
        self.layout.setdefault("widgets", {})
        self.layout.setdefault("objects", {})

        # ── live session accounting (not persisted to disk; rebuilt per run) ──
        self.coal_bag_count: int = -1  # unknown after (re)connect -> err coal-first
        self.coal_deposited: int = 0
        self.ore_deposited: int = 0
        self.bars_collected: int = 0
        self.session_start: float = time.time()
        # sticky bar type: remembered once auto-detected, so XP + policy survive the
        # empty-inventory ticks where inventory-only detection would return None.
        self.last_bar_type: Optional[str] = None
        # rolling XP/hr sampler: (timestamp, cumulative bars). Throttled + pruned.
        self.samples: list = [(self.session_start, 0)]
        self._last_sample_t: float = 0.0

    # ── json helpers ──────────────────────────────────────────────────────────
    @staticmethod
    def _load(path: Path, default: Any) -> Any:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return default

    @staticmethod
    def _save(path: Path, data: Any) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)

    def save_hotspots(self) -> None:
        self._save(self.hotspots_path, self.hotspots)

    def save_layout(self) -> None:
        self._save(self.layout_path, self.layout)

    # ── session ───────────────────────────────────────────────────────────────
    def session_seconds(self) -> float:
        return time.time() - self.session_start

    # ── rolling XP/hr sampler ───────────────────────────────────────────────────
    # A single alternating readout: 2-minute reactive window, then longer averages
    # at 10-minute increments up to the elapsed session, plus session-cumulative —
    # cycled one at a time in the HUD rather than stacked as separate rows.
    SAMPLE_THROTTLE_S = 3.0     # don't record more often than this
    SAMPLE_MAX_AGE_S = 70 * 60  # keep ~70 min of history (covers up to 60m window)
    ROTATE_SECONDS = 5.0        # switch displayed window every N seconds
    SHORT_WINDOW_MIN = 2

    def record_sample(self, bars_collected: int) -> None:
        now = time.time()
        if now - self._last_sample_t < self.SAMPLE_THROTTLE_S:
            return
        self._last_sample_t = now
        self.samples.append((now, bars_collected))
        cutoff = now - self.SAMPLE_MAX_AGE_S
        if self.samples[0][0] < cutoff:
            self.samples = [sm for sm in self.samples if sm[0] >= cutoff] or self.samples[-1:]

    def _active_windows(self, elapsed_min: float):
        """(label, minutes|None) list; None = session cumulative. Only windows with
        enough elapsed time are included, so we never show a 20m average at 8m in."""
        windows = []
        if elapsed_min >= self.SHORT_WINDOW_MIN:
            windows.append(("2m", self.SHORT_WINDOW_MIN))
        w = 10
        while w <= elapsed_min:
            windows.append((f"{w}m", w))
            w += 10
        windows.append(("cum", None))
        return windows

    def rolling_xp_line(self, xp_per_bar: float) -> str:
        if xp_per_bar <= 0:
            return ""
        now = time.time()
        elapsed_min = (now - self.session_start) / 60.0
        if elapsed_min < 0.2:
            return ""
        windows = self._active_windows(elapsed_min)
        label, minutes = windows[int(now / self.ROTATE_SECONDS) % len(windows)]
        if minutes is None:
            base_t, base_bars = self.session_start, 0
        else:
            cutoff = now - minutes * 60
            base = next((sm for sm in self.samples if sm[0] >= cutoff), self.samples[0])
            base_t, base_bars = base
        span_h = (now - base_t) / 3600.0
        if span_h <= 0:
            return ""
        xp_hr = (self.bars_collected - base_bars) * xp_per_bar / span_h
        return f"XP/hr ({label}): {xp_hr:,.0f}"

    def ctx(self, bar_type_config: str = "AUTO",
            coffer_low_minutes: int = 20, coffer_critical_gp: int = 0) -> Dict[str, Any]:
        """The context dict passed into policy_bf.build_snapshot each tick."""
        return {
            "coal_bag_count": self.coal_bag_count,
            "bar_type_config": bar_type_config,
            # sticky type is only a FALLBACK for empty-inventory ticks; live detection
            # still wins, so switching ore (adamantite -> mithril) updates immediately.
            "last_bar_type": self.last_bar_type,
            "coffer_low_minutes": coffer_low_minutes,
            "coffer_critical_gp": coffer_critical_gp,
            "session_seconds": self.session_seconds(),
            "coal_deposited": self.coal_deposited,
            "ore_deposited": self.ore_deposited,
            "bars_collected": self.bars_collected,
        }

    # ── discovery (bank item / widget bounds) ──────────────────────────────────
    def record_discovered(self, discovered: Dict[str, Any]) -> None:
        changed = False
        for it in discovered.get("bankItems", []) or []:
            iid = it.get("id")
            if iid is None:
                continue
            self.layout["bankItems"][str(iid)] = {
                "x": it.get("x"), "y": it.get("y"), "w": it.get("w"), "h": it.get("h"),
            }
            changed = True
        for key, box in (discovered.get("widgets") or {}).items():
            # bankClose comes from the bridge's smallest-area "Close" scan (the X);
            # keep a button-like sanity filter so a stray oversized/scrollbar match
            # can never be stored.
            if key == "bankClose" and not _button_like(box):
                continue
            self.layout["widgets"][key] = box
            changed = True
        if changed:
            self.save_layout()

    def layout_for_policy(self) -> Dict[str, Any]:
        return {
            "bankItems": self.layout.get("bankItems", {}),
            "widgets": self.layout.get("widgets", {}),
            "objects": self.layout.get("objects", {}),
            "hotspots": self.hotspots,
        }

    # ── hotspots (learned standing tiles) ──────────────────────────────────────
    def record_hotspot(self, canonical_id: int, x: int, y: int, plane: int = 0) -> None:
        self.hotspots[str(canonical_id)] = {"x": x, "y": y, "plane": plane}
        self.save_hotspots()

    # ── action log + session counters ──────────────────────────────────────────
    def log_action(self, event: Dict[str, Any]) -> None:
        try:
            with open(self.actions_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": time.time(), **event}) + "\n")
        except OSError:
            pass
        self._apply_event(event)

    def _apply_event(self, event: Dict[str, Any]) -> None:
        """Update coal-bag count + session counters from a menu click (ported from
        the Java plugin's onMenuOptionClicked / deposit accounting)."""
        if event.get("name") != "menuOptionClicked":
            return
        option = (event.get("option") or "").lower()
        target = (event.get("target") or "").lower()

        # Coal bag Fill/Empty — match by TARGET TEXT ("Open coal bag"), NOT item_id.
        # The menu event's id field is a widget param (e.g. 2), never the coal-bag
        # item id, so item-id matching never fired: the bag count stayed unknown and
        # the policy looped on coal, never advancing to ore. (12020 was GEM_BAG too.)
        if "coal bag" in target:
            if option == "fill":
                self.coal_bag_count = ids.COAL_BAG_CAPACITY   # bag now full
            elif option == "empty":
                self.coal_deposited += max(0, self.coal_bag_count)
                self.coal_bag_count = 0

        # Belt deposits + bar collection accounting for HUD rates.
        if "coal" in target and ("put" in option or "use" in option or "deposit" in option):
            self.coal_deposited += ids.COAL_INV_LOAD
        if "take" in option and "dispenser" in target:
            self.bars_collected += 28

    def reset_session(self) -> None:
        self.coal_bag_count = -1
        self.coal_deposited = 0
        self.ore_deposited = 0
        self.bars_collected = 0
        self.session_start = time.time()
        self.samples = [(self.session_start, 0)]
        self._last_sample_t = 0.0
