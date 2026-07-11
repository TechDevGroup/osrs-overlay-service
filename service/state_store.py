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

    def ctx(self, bar_type_config: str = "AUTO",
            coffer_low_minutes: int = 20, coffer_critical_gp: int = 0) -> Dict[str, Any]:
        """The context dict passed into policy_bf.build_snapshot each tick."""
        return {
            "coal_bag_count": self.coal_bag_count,
            "bar_type_config": bar_type_config,
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
            self.layout["widgets"][key] = box
            changed = True
        if changed:
            self.save_layout()

    def layout_for_policy(self) -> Dict[str, Any]:
        return {
            "bankItems": self.layout.get("bankItems", {}),
            "widgets": self.layout.get("widgets", {}),
            "objects": self.layout.get("objects", {}),
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
        item_id = event.get("id")
        target = (event.get("target") or "").lower()

        if item_id in (ids.ITEM_COAL_BAG, ids.ITEM_COAL_BAG_FULL):
            if option == "fill":
                # Absorbs up to capacity; we don't know exact inv coal here, assume full.
                self.coal_bag_count = ids.COAL_BAG_CAPACITY
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
