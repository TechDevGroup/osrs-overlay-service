"""Blast Furnace policy — the LOGIC half of the thin-client overlay.

Faithful Python port of BFPolicy.java / BFStateSnapshot.java / BarType.java /
BFAction.java / BFGuidance.java from TechDevGroup/runelite-blast-furnace-helper.

`derive(snapshot)` is a PURE, idempotent function: given only the currently
observed game state it returns the single correct next action. It holds no memory
of a step index, so it self-corrects when the player arrives mid-cycle.

`build_directives(snapshot, guidance, layout)` turns that guidance plus the HUD
math into the wire-protocol directive list the plugin renders.

This module is HOT-RELOADED (importlib.reload) by the server on file change, so
game logic can iterate without restarting the RuneLite client. Keep it free of
long-lived state — all persistence lives in state_store.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional

from . import ids


# ── BarType ──────────────────────────────────────────────────────────────────
class BarType(Enum):
    #   name, ore_item, bar_item, coal_per_bar, furnace_ore_varbit, furnace_bar_varbit, xp_per_bar
    IRON = ("Iron", ids.ITEM_IRON_ORE, ids.ITEM_IRON_BAR, 0,
            ids.VAR_FURNACE_IRON_ORE, ids.VAR_FURNACE_IRON_BARS, 12.5)
    STEEL = ("Steel", ids.ITEM_IRON_ORE, ids.ITEM_STEEL_BAR, 1,
             ids.VAR_FURNACE_IRON_ORE, ids.VAR_FURNACE_STEEL_BARS, 17.5)
    MITHRIL = ("Mithril", ids.ITEM_MITHRIL_ORE, ids.ITEM_MITHRIL_BAR, 2,
               ids.VAR_FURNACE_MITHRIL_ORE, ids.VAR_FURNACE_MITHRIL_BARS, 30.0)
    ADAMANTITE = ("Adamantite", ids.ITEM_ADAMANTITE_ORE, ids.ITEM_ADAMANTITE_BAR, 3,
                  ids.VAR_FURNACE_ADAMANTITE_ORE, ids.VAR_FURNACE_ADAMANTITE_BARS, 37.5)
    RUNITE = ("Runite", ids.ITEM_RUNITE_ORE, ids.ITEM_RUNITE_BAR, 4,
              ids.VAR_FURNACE_RUNITE_ORE, ids.VAR_FURNACE_RUNITE_BARS, 50.0)

    @property
    def display_name(self) -> str:
        return self.value[0]

    @property
    def ore_item_id(self) -> int:
        return self.value[1]

    @property
    def bar_item_id(self) -> int:
        return self.value[2]

    @property
    def coal_per_bar(self) -> int:
        return self.value[3]

    @property
    def xp_per_bar(self) -> float:
        return self.value[6]

    @property
    def furnace_ore_varbit(self) -> int:
        return self.value[4]

    @property
    def furnace_bar_varbit(self) -> int:
        return self.value[5]


# ── BFAction ─────────────────────────────────────────────────────────────────
class ObjTarget(Enum):
    NONE = 0
    CONVEYOR = 1
    DISPENSER = 2
    BANK_CHEST = 3
    COFFER = 4


class BFAction(Enum):
    #   label, object target
    IDLE = ("Idle", ObjTarget.NONE)
    WITHDRAW_COINS = ("Withdraw coins", ObjTarget.NONE)
    REFILL_COFFER = ("Refill coffer", ObjTarget.COFFER)
    FILL_COAL_BAG = ("Fill coal bag", ObjTarget.NONE)
    WITHDRAW_COAL = ("Withdraw coal", ObjTarget.NONE)
    WITHDRAW_ORE = ("Withdraw ore", ObjTarget.NONE)
    GO_TO_BELT = ("Go to conveyor belt", ObjTarget.CONVEYOR)
    EMPTY_COAL_BAG = ("Empty coal bag", ObjTarget.NONE)
    DEPOSIT_COAL = ("Deposit coal on belt", ObjTarget.CONVEYOR)
    DEPOSIT_ORE = ("Deposit ore on belt", ObjTarget.CONVEYOR)
    COLLECT_BARS = ("Collect bars", ObjTarget.DISPENSER)
    WAIT_SMELT = ("Smelting...", ObjTarget.DISPENSER)
    DEPOSIT_BARS = ("Deposit bars", ObjTarget.BANK_CHEST)
    GO_TO_BANK = ("Go to bank", ObjTarget.BANK_CHEST)

    @property
    def label(self) -> str:
        return self.value[0]

    @property
    def object_target(self) -> ObjTarget:
        return self.value[1]


# ── BFGuidance ───────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class BFGuidance:
    action: BFAction
    bank_item_id: int = -1
    inv_item_id: int = -1

    @staticmethod
    def of(action: BFAction) -> "BFGuidance":
        return BFGuidance(action, -1, -1)

    @staticmethod
    def bank_item(action: BFAction, item_id: int) -> "BFGuidance":
        return BFGuidance(action, item_id, -1)

    @staticmethod
    def inv_item(action: BFAction, item_id: int) -> "BFGuidance":
        return BFGuidance(action, -1, item_id)

    @property
    def object_target(self) -> ObjTarget:
        return self.action.object_target

    @property
    def label(self) -> str:
        return self.action.label


# ── BFStateSnapshot ──────────────────────────────────────────────────────────
@dataclass(frozen=True)
class BFStateSnapshot:
    bar_type: Optional[BarType] = None
    bank_open: bool = False

    # Inventory (observed)
    inv_coal: int = 0
    inv_ore: int = 0
    inv_bars: int = 0
    free_slots: int = 28
    coal_bag_has_coal: bool = False
    coal_bag_full: bool = False

    # Location context
    at_bank: bool = False
    at_belt: bool = False

    # Furnace (varbits)
    furnace_coal: int = 0
    furnace_ore: int = 0
    furnace_bars: int = 0
    dispenser_state: int = 0

    # Coffer
    holding_coins: bool = False
    coffer_low: bool = False
    coffer_critical: bool = False

    # HUD accounting (server-maintained session context; not used by derive()).
    coffer_balance: int = -1
    session_seconds: float = 0.0
    coal_deposited: int = 0
    ore_deposited: int = 0
    bars_collected: int = 0
    rolling_xp_line: str = ""  # server-computed alternating rolling XP/hr readout

    def replace(self, **kw: Any) -> "BFStateSnapshot":
        return dataclasses.replace(self, **kw)


# ── BFPolicy.derive (1:1 port) ───────────────────────────────────────────────
def derive(s: BFStateSnapshot) -> BFGuidance:
    bt = s.bar_type
    if bt is None:
        return BFGuidance.of(BFAction.IDLE)
    ratio = bt.coal_per_bar

    # 1. Coffer critical overrides the smithing loop.
    if s.coffer_critical:
        if s.bank_open:
            return BFGuidance.bank_item(BFAction.WITHDRAW_COINS, ids.ITEM_COINS)
        if s.holding_coins:
            return BFGuidance.of(BFAction.REFILL_COFFER)
        # No coins available — fall through and keep smithing.

    # 2. Finished bars already in the inventory -> bank them.
    if s.inv_bars > 0:
        if s.bank_open:
            return BFGuidance.of(BFAction.DEPOSIT_BARS)
        return BFGuidance.of(BFAction.GO_TO_BANK)

    # 3. AT THE BANK (interface open) -> acquire the next material.
    if s.bank_open:
        return _bank_acquire(s, bt, ratio)

    # 4. AT THE BELT -> unload only.
    if s.at_belt:
        if s.inv_coal > 0:
            return BFGuidance.of(BFAction.DEPOSIT_COAL)
        if s.inv_ore > 0:
            return BFGuidance.of(BFAction.DEPOSIT_ORE)
        if s.coal_bag_has_coal and s.free_slots > 0:
            return BFGuidance.inv_item(BFAction.EMPTY_COAL_BAG, ids.ITEM_COAL_BAG)
        # Nothing left to deposit -> fall through to the return-leg tail.
    # 5. AT THE BANK CHEST but interface closed.
    elif s.at_bank:
        if ratio > 0 and not s.coal_bag_full and s.inv_coal > 0:
            return BFGuidance.inv_item(BFAction.FILL_COAL_BAG, ids.ITEM_COAL_BAG)
        # Coal trip: the observed routine fills the bag with the bank CLOSED, then
        # REOPENS to withdraw the final loose coal load. If the bag is full but the
        # furnace still wants a loose load and we don't have one yet, guide back to
        # the bank (reopen) rather than to the belt — so the coal ghost shows for
        # that final withdrawal instead of jumping straight to GO_TO_BELT.
        if (ratio > 0 and s.coal_bag_full and s.inv_coal < ids.COAL_INV_LOAD
                and _furnace_needs_loose_coal(s, ratio)):
            return BFGuidance.of(BFAction.GO_TO_BANK)
        if s.inv_coal > 0 or s.inv_ore > 0 or s.coal_bag_has_coal:
            return BFGuidance.of(BFAction.GO_TO_BELT)
        return BFGuidance.of(BFAction.GO_TO_BANK)
    # 6. EN ROUTE -> carry any load to the belt.
    elif s.inv_coal > 0 or s.inv_ore > 0 or s.coal_bag_has_coal:
        return BFGuidance.of(BFAction.GO_TO_BELT)

    # ── Return-leg tail (empty inventory). ──
    # 7. Collect bars while passing the dispenser — strictly before any bank trip.
    if s.furnace_bars >= 1 and s.free_slots > 0:
        return BFGuidance.of(BFAction.COLLECT_BARS)
    # 8. Coffer low and carrying coins -> top it up on the way past.
    if s.coffer_low and s.holding_coins:
        return BFGuidance.of(BFAction.REFILL_COFFER)
    # 9. Nothing to do -> head to the bank to restock.
    return BFGuidance.of(BFAction.GO_TO_BANK)


def _bank_acquire(s: BFStateSnapshot, bt: BarType, ratio: int) -> BFGuidance:
    # Iron (ratio 0) uses no coal at all.
    if ratio <= 0:
        if s.inv_ore < ids.ORE_LOAD:
            return BFGuidance.bank_item(BFAction.WITHDRAW_ORE, bt.ore_item_id)
        return BFGuidance.of(BFAction.GO_TO_BELT)

    # 1. Fill the coal bag first (coal-before-ore).
    if not s.coal_bag_full:
        if s.inv_coal <= 0:
            return BFGuidance.bank_item(BFAction.WITHDRAW_COAL, ids.ITEM_COAL)
        return BFGuidance.inv_item(BFAction.FILL_COAL_BAG, ids.ITEM_COAL_BAG)

    # 2. Bag is full. Does the furnace need a loose coal load too (a "coal trip")?
    if _furnace_needs_loose_coal(s, ratio):
        if s.inv_coal < ids.COAL_INV_LOAD:
            return BFGuidance.bank_item(BFAction.WITHDRAW_COAL, ids.ITEM_COAL)
        return BFGuidance.of(BFAction.GO_TO_BELT)

    # 3. Ore trip: bag full and no loose coal needed -> withdraw ore.
    if s.inv_ore < ids.ORE_LOAD:
        return BFGuidance.bank_item(BFAction.WITHDRAW_ORE, bt.ore_item_id)
    return BFGuidance.of(BFAction.GO_TO_BELT)


def _furnace_needs_loose_coal(s: BFStateSnapshot, ratio: int) -> bool:
    # furnaceCoal + bagCapacity < ratio * (furnaceOre + ORE_LOAD).
    # Full ORE_LOAD (not +1) makes a small residual coal amount read as effectively
    # empty. Adamantite (ratio 3, ORE_LOAD 27, bag 27): switch at fcoal < 54, so
    # fcoal~=2 -> 2-coal trip, fcoal~=56 -> 1-coal+1-ore trip.
    return s.furnace_coal + ids.COAL_BAG_CAPACITY < ratio * (s.furnace_ore + ids.ORE_LOAD)


# ── HUD (trip computer) ──────────────────────────────────────────────────────
def _hud_lines(s: BFStateSnapshot) -> List[str]:
    lines: List[str] = []
    bt = s.bar_type
    lines.append(f"Bar: {bt.display_name if bt else 'Unknown'}")

    hours = s.session_seconds / 3600.0
    if hours > 0.01:
        bars_hr = round(s.bars_collected / hours)
        coal_hr = round(s.coal_deposited / hours)
        ore_hr = round(s.ore_deposited / hours)
        lines.append(f"Bars/hr: {bars_hr}")
        lines.append(f"Coal/hr: {coal_hr}")
        lines.append(f"Ore/hr: {ore_hr}")

    # Single rolling XP/hr line that alternates window (2m / 10m / 20m / ... / cum)
    # instead of stacking one row per interval. Computed server-side (needs time).
    if s.rolling_xp_line:
        lines.append(s.rolling_xp_line)

    if s.coffer_balance >= 0:
        mins = s.coffer_balance / ids.COFFER_DRAIN_PER_MINUTE if s.coffer_balance > 0 else 0.0
        tag = ""
        if s.coffer_critical:
            tag = " EMPTY!"
        elif s.coffer_low:
            tag = " LOW"
        lines.append(f"Coffer: {s.coffer_balance:,} gp (~{mins:.0f}m){tag}")
    return lines


# ── Directive assembly ───────────────────────────────────────────────────────
_OBJ_IDS = {
    ObjTarget.CONVEYOR: [ids.CONVEYOR_BELT],
    ObjTarget.DISPENSER: [ids.DISPENSER_FULL, ids.DISPENSER_COOLED],
    ObjTarget.BANK_CHEST: [ids.BANK_CHEST],
    ObjTarget.COFFER: list(ids.COFFER_IDS),
}

# The learned standing tile (hotspot) the user clicks to run to for each target,
# keyed by the canonical object id used in the hotspots store.
_TARGET_HOTSPOT_ID = {
    ObjTarget.DISPENSER: ids.DISPENSER_BASE,
    ObjTarget.CONVEYOR: ids.CONVEYOR_BELT,
    ObjTarget.BANK_CHEST: ids.BANK_CHEST,
}

COLOR_PRIMARY = "#ffcc00"      # bright: the next click
COLOR_OBJECT = "#00ff88"       # world object outline
COLOR_SECONDARY = "#88ffcc00"  # dim/translucent: also-needed this phase
COLOR_PREDICT = "#c800ff"      # predicted ghost (bank closed) — primary
COLOR_PREDICT_2 = "#80c800ff"  # predicted ghost — companion material (dimmed)
COLOR_COFFER = "#ff4444"
COLOR_CLOSE = "#ff8800"       # bank close — actionable (time to leave)
COLOR_CLOSE_DIM = "#80ff8800" # bank close — prestaged position (dim)
COLOR_TILE = "#ffcc00"        # standing/click tile the user runs to


def build_directives(
    s: BFStateSnapshot,
    guidance: BFGuidance,
    layout: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Pure map from (snapshot, guidance) -> wire directive list.

    Emits: the next-click highlight (bank item / inv item / object), companion
    highlights so BOTH coal AND the primary ore are lit in the bank-acquire
    phases (fixes the live bug where only coal highlighted), a world arrow at the
    target object when its scene location is known, the coffer highlight when
    low/critical, the bank close button when leaving the bank, predicted
    bank-item ghosts when the bank is closed, and the trip-computer HUD.
    """
    layout = layout or {}
    directives: List[Dict[str, Any]] = []
    bt = s.bar_type
    action = guidance.action

    # --- Primary next-click target -------------------------------------------
    if guidance.bank_item_id >= 0:
        directives.append(_bank_item(guidance.bank_item_id, COLOR_PRIMARY, action.label, s, layout))
    if guidance.inv_item_id >= 0:
        directives.append({"kind": "invItem", "id": guidance.inv_item_id,
                           "color": COLOR_PRIMARY, "label": action.label})

    # --- Companion coal+ore highlights in the bank-acquire phases ------------
    # A bank withdrawal of coal or ore means we are assembling a coal+ore load;
    # light the OTHER material too (dimmed) so the whole phase is visible. This
    # guarantees adamantite (ratio 3, ore 449) shows an ore highlight in the
    # coal+ore phase, not coal alone.
    if bt is not None and bt.coal_per_bar > 0:
        if action == BFAction.WITHDRAW_COAL:
            directives.append(_bank_item(bt.ore_item_id, COLOR_SECONDARY, "then: ore", s, layout))
        elif action == BFAction.WITHDRAW_ORE:
            directives.append(_bank_item(ids.ITEM_COAL, COLOR_SECONDARY, "coal (in bag)", s, layout))

    # --- Deposit bars: highlight the bar in the inventory so the user knows what
    #     to click the moment the bank opens (inventory is visible before/while the
    #     bank UI is up, so this shows through the open). -----------------------
    if action == BFAction.DEPOSIT_BARS and bt is not None:
        directives.append({"kind": "invItem", "id": bt.bar_item_id,
                           "color": COLOR_PRIMARY, "label": "Deposit bars"})

    # --- Object highlight for the action's world target ----------------------
    target = action.object_target
    if target in _OBJ_IDS:
        loc = _object_loc(target, layout)
        for oid in _OBJ_IDS[target]:
            directives.append({"kind": "object", "id": oid,
                               "color": COLOR_OBJECT, "label": action.label, "outline": True})
        if loc is not None:
            directives.append({"kind": "worldArrow", "plane": loc.get("plane", 0),
                               "x": loc["x"], "y": loc["y"], "color": COLOR_OBJECT})

    # --- Standing tile the user clicks to run to for this action (learned) -----
    # e.g. the run-by tile at the bar dispenser for COLLECT_BARS. Hotspots are
    # stored but were never emitted as tile directives (reported: never showed).
    hs_id = _TARGET_HOTSPOT_ID.get(target)
    if hs_id is not None:
        hs = (layout.get("hotspots") or {}).get(str(hs_id))
        if hs is not None:
            directives.append({"kind": "tile", "plane": hs.get("plane", 0),
                               "x": hs["x"], "y": hs["y"], "color": COLOR_TILE,
                               "fill": "#33ffcc00", "label": action.label})

    # --- Coffer highlight when low / critical --------------------------------
    if s.coffer_critical or (s.coffer_low and s.holding_coins):
        for oid in ids.COFFER_IDS:
            directives.append({"kind": "object", "id": oid, "color": COLOR_COFFER,
                               "label": "Coffer", "outline": True})

    # --- Bank close button: prestage its position BEFORE the UI opens (predicted
    #     from cached bounds) and show it live WHILE the UI is up. Dim as a
    #     position hint; brighten when it's actually time to leave. ------------
    # Use ONLY the op-text-discovered "bankClose" bounds (reported by the bridge's
    # widgetFind scan). We do NOT fall back to a guessed child — child 12.13 was a
    # bad guess that is actually the scrollbar (reported). Until the bridge reports
    # bankClose, we draw nothing rather than the wrong widget. Bounds are live while
    # the UI is up and last-seen (cached) before it opens, so one directive covers
    # both "before the UI" and "while the UI is up".
    widgets_layout = layout.get("widgets") or {}
    close_bounds = widgets_layout.get("bankClose")
    in_bank_ctx = s.bank_open or s.at_bank or action == BFAction.GO_TO_BANK
    time_to_leave = s.bank_open and target != ObjTarget.NONE and action not in (
        BFAction.WITHDRAW_COAL, BFAction.WITHDRAW_ORE, BFAction.WITHDRAW_COINS,
        BFAction.FILL_COAL_BAG, BFAction.DEPOSIT_BARS,
    )
    if in_bank_ctx and close_bounds is not None:
        cc = COLOR_CLOSE if time_to_leave else COLOR_CLOSE_DIM
        directives.append({"kind": "widgetPredicted", "group": ids.BANK_GROUP_ID,
                           "child": close_bounds.get("child", -1),
                           "x": close_bounds["x"], "y": close_bounds["y"],
                           "color": cc, "label": "Close bank"})

    # --- Predicted bank-item ghosts when heading to the bank (bank closed) -----
    # Ghost the WHOLE upcoming withdrawal, not just the single next item. The
    # derived "next" item flickers to ore only briefly in the coal+ore phase, so
    # a single-item prediction shows coal almost always and the ore ghost never
    # appears (reported bug). Emit primary + companion (coal<->ore) ghosts, same
    # pairing as the open-bank companion logic above.
    # Also fire on the return-leg actions (COLLECT_BARS / REFILL_COFFER), not just
    # GO_TO_BANK: while running back past the dispenser the player is still headed
    # to the bank, so the dispenser highlight should NOT suppress the upcoming-
    # withdrawal ghosts (reported: dispenser overrides next highlights until near
    # the chest).
    if not s.bank_open and action in (
        BFAction.GO_TO_BANK, BFAction.COLLECT_BARS, BFAction.REFILL_COFFER,
    ):
        # Classify the upcoming bank visit by deriving as-if the coal bag were
        # already full — this skips the transient fill-bag micro-step so the result
        # is the ACTUAL withdrawal: a coal trip -> WITHDRAW_COAL, the ore trip ->
        # WITHDRAW_ORE. Ghost that primary prominently; on the ore trip also show the
        # coal-bag top-up as a dim companion. This shows the right material for the
        # trip you're on (incl. the reopen-for-final-coal), without over-showing ore
        # on pure coal trips.
        probe = s.replace(bank_open=True, inv_bars=0,
                          coal_bag_full=True, coal_bag_has_coal=True)
        pred = derive(probe)
        ghosts = []  # (item_id, color)
        if pred.bank_item_id >= 0:
            ghosts.append((pred.bank_item_id, COLOR_PREDICT))
        if pred.action == BFAction.WITHDRAW_ORE and bt is not None and bt.coal_per_bar > 0:
            ghosts.append((ids.ITEM_COAL, COLOR_PREDICT_2))
        seen = set()
        for gid, gcolor in ghosts:
            if gid in seen:
                continue
            seen.add(gid)
            bounds = _bank_bounds(gid, layout)
            if bounds is not None:
                directives.append({"kind": "bankItemPredicted", "id": gid,
                                   "x": bounds["x"], "y": bounds["y"], "color": gcolor})

    # --- HUD (always) ---------------------------------------------------------
    directives.append({"kind": "text", "anchor": "topRight", "lines": _hud_lines(s)})
    return directives


def _bank_item(item_id: int, color: str, label: str, s: BFStateSnapshot,
               layout: Dict[str, Any]) -> Dict[str, Any]:
    """Live bank item when the bank is open, else a cached-layout predicted ghost."""
    if s.bank_open:
        return {"kind": "bankItem", "id": item_id, "color": color, "label": label}
    bounds = _bank_bounds(item_id, layout)
    if bounds is not None:
        return {"kind": "bankItemPredicted", "id": item_id,
                "x": bounds["x"], "y": bounds["y"], "color": color}
    return {"kind": "bankItem", "id": item_id, "color": color, "label": label}


def _bank_bounds(item_id: int, layout: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    return (layout.get("bankItems") or {}).get(str(item_id))


def _object_loc(target: ObjTarget, layout: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    return (layout.get("objects") or {}).get(target.name)


# ── State extraction: raw wire state -> BFStateSnapshot ──────────────────────
# Ported from BlastFurnaceHelperPlugin.buildSnapshot / detectBarType / coffer &
# location helpers. Kept here (hot-reloadable) because it is domain logic.

# Standing-position anchors + Chebyshev radius (BFConstants).
BANK_ANCHOR_X = 1948
BANK_ANCHOR_Y = 4957
BELT_ANCHOR_X = 1940
BELT_ANCHOR_Y = 4965
PROXIMITY_RADIUS = 3

_BAR_CONFIG = {
    "IRON": BarType.IRON, "STEEL": BarType.STEEL, "MITHRIL": BarType.MITHRIL,
    "ADAMANTITE": BarType.ADAMANTITE, "RUNITE": BarType.RUNITE,
}


def _count_item(inv: List[Dict[str, Any]], item_id: int) -> int:
    return sum(i.get("qty", 0) for i in inv if i.get("id") == item_id)


def _detect_bar_type(inv: List[Dict[str, Any]]) -> Optional[BarType]:
    has_coal = _count_item(inv, ids.ITEM_COAL) > 0
    # Non-iron ores first (specific detection).
    for bt in (BarType.MITHRIL, BarType.ADAMANTITE, BarType.RUNITE):
        if _count_item(inv, bt.ore_item_id) > 0:
            return bt
    if has_coal and _count_item(inv, ids.ITEM_IRON_ORE) > 0:
        return BarType.STEEL
    if _count_item(inv, ids.ITEM_IRON_ORE) > 0:
        return BarType.IRON
    return None


def _free_slots(inv: List[Dict[str, Any]]) -> int:
    used = sum(1 for i in inv if i.get("id", 0) > 0 and i.get("qty", 0) > 0)
    return max(0, 28 - used)


def _near(px: int, py: int, ax: int, ay: int) -> bool:
    return abs(px - ax) <= PROXIMITY_RADIUS and abs(py - ay) <= PROXIMITY_RADIUS


def build_snapshot(raw: Dict[str, Any], ctx: Dict[str, Any]) -> BFStateSnapshot:
    """Build the pure snapshot from a raw `state` wire message plus session ctx.

    ctx keys: coal_bag_count(int,-1=unknown), bar_type_config(str|"AUTO"),
    coffer_low_minutes, coffer_critical_gp, session_seconds, coal_deposited,
    ore_deposited, bars_collected.
    """
    inv = raw.get("inv") or []
    varbits = {int(k): v for k, v in (raw.get("varbits") or {}).items()}
    bank = raw.get("bank") or {}
    bank_open = bool(bank.get("open"))
    player = raw.get("player") or {}
    objects = raw.get("objects") or []

    # Bar type: explicit config overrides auto-detection.
    cfg = (ctx.get("bar_type_config") or "AUTO").upper()
    bt = _BAR_CONFIG.get(cfg) if cfg != "AUTO" else _detect_bar_type(inv)

    inv_coal = _count_item(inv, ids.ITEM_COAL)
    inv_ore = _count_item(inv, bt.ore_item_id) if bt else 0
    inv_bars = _count_item(inv, bt.bar_item_id) if bt else 0
    free = _free_slots(inv)

    furnace_coal = varbits.get(ids.VAR_FURNACE_COAL, 0)
    furnace_ore = varbits.get(bt.furnace_ore_varbit, 0) if bt else 0
    furnace_bars = varbits.get(bt.furnace_bar_varbit, 0) if bt else 0
    dispenser_state = varbits.get(ids.VAR_DISPENSER_STATE, 0)

    # Coal bag: session-tracked count (-1 unknown -> err coal-first). A full
    # coal-bag item id in inventory forces full.
    count = ctx.get("coal_bag_count", -1)
    coal_bag_full = count >= ids.COAL_BAG_CAPACITY or _count_item(inv, ids.ITEM_COAL_BAG_FULL) > 0
    coal_bag_has_coal = count != 0 or _count_item(inv, ids.ITEM_COAL_BAG_FULL) > 0

    # Coffer.
    coffer_balance = varbits.get(ids.VAR_COFFER, -1)
    critical_gp = ctx.get("coffer_critical_gp", 0)
    low_minutes = ctx.get("coffer_low_minutes", 20)
    coffer_critical = coffer_balance >= 0 and coffer_balance <= critical_gp
    mins_left = (coffer_balance / ids.COFFER_DRAIN_PER_MINUTE) if coffer_balance > 0 else 0.0
    coffer_low = (coffer_balance >= 0 and not coffer_critical and mins_left < low_minutes)
    holding_coins = _count_item(inv, ids.ITEM_COINS) > 0

    # Location context.
    px, py = player.get("x", 0), player.get("y", 0)
    near_bank_obj = any(o.get("id") == ids.BANK_CHEST for o in objects)
    near_belt_obj = any(o.get("id") == ids.CONVEYOR_BELT for o in objects)
    at_bank = bank_open or (near_bank_obj and _near(px, py, BANK_ANCHOR_X, BANK_ANCHOR_Y)) \
        or _near(px, py, BANK_ANCHOR_X, BANK_ANCHOR_Y)
    at_belt = (near_belt_obj and _near(px, py, BELT_ANCHOR_X, BELT_ANCHOR_Y)) \
        or _near(px, py, BELT_ANCHOR_X, BELT_ANCHOR_Y)

    return BFStateSnapshot(
        bar_type=bt, bank_open=bank_open,
        inv_coal=inv_coal, inv_ore=inv_ore, inv_bars=inv_bars, free_slots=free,
        coal_bag_has_coal=coal_bag_has_coal, coal_bag_full=coal_bag_full,
        at_bank=at_bank, at_belt=at_belt,
        furnace_coal=furnace_coal, furnace_ore=furnace_ore, furnace_bars=furnace_bars,
        dispenser_state=dispenser_state,
        holding_coins=holding_coins, coffer_low=coffer_low, coffer_critical=coffer_critical,
        coffer_balance=coffer_balance,
        session_seconds=ctx.get("session_seconds", 0.0),
        coal_deposited=ctx.get("coal_deposited", 0),
        ore_deposited=ctx.get("ore_deposited", 0),
        bars_collected=ctx.get("bars_collected", 0),
    )


# The subscription the service declares on hello (BF domain).
SUBSCRIBE = {
    "t": "subscribe",
    "proto": 1,
    "containers": ["inventory", "bank"],
    "varbits": [
        ids.VAR_COFFER, ids.VAR_FURNACE_COAL,
        ids.VAR_FURNACE_IRON_ORE, ids.VAR_FURNACE_MITHRIL_ORE,
        ids.VAR_FURNACE_ADAMANTITE_ORE, ids.VAR_FURNACE_RUNITE_ORE,
        ids.VAR_FURNACE_IRON_BARS, ids.VAR_FURNACE_STEEL_BARS,
        ids.VAR_FURNACE_MITHRIL_BARS, ids.VAR_FURNACE_ADAMANTITE_BARS,
        ids.VAR_FURNACE_RUNITE_BARS, ids.VAR_DISPENSER_STATE,
    ],
    "varps": [],
    "objects": [
        ids.CONVEYOR_BELT, ids.DISPENSER_FULL, ids.DISPENSER_COOLED, ids.DISPENSER_BASE,
        ids.BANK_CHEST, ids.COFFER_EMPTY, ids.COFFER_FULL, ids.COFFER_ACTIVE,
    ],
    "npcs": [],
    "widgets": [],
    # Ask the bridge to DISCOVER the bank close button by scanning group 12 for the
    # child whose menu action is "Close" (no guessed child id — 12.13 is the
    # scrollbar). Reported back as discovered.widgets["bankClose"] {x,y,w,h,child}.
    "widgetFind": [{"group": ids.BANK_GROUP_ID, "action": "Close", "as": "bankClose"}],
    "events": ["menuOptionClicked", "animationChanged"],
    "tickState": True,
}
