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
    inv_slots: tuple = ()      # (slot, item_id) for wrong-item detection

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
COLOR_WRONG = "#ff2020"       # wrong item in inventory (foreign ore/bar)

# every ore + bar id across bar types — used to flag foreign ores/bars in inventory
_ALL_ORE_BAR_IDS = {i for bt in BarType for i in (bt.ore_item_id, bt.bar_item_id)}


def build_directives(
    s: BFStateSnapshot,
    guidance: BFGuidance,
    layout: Optional[Dict[str, Any]] = None,
    plan: Optional[List[Optional[Dict[str, Any]]]] = None,
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

    # The whole SERIAL banking sequence — approach, open, the close-to-fill-bag and
    # reopen sub-steps, and every withdraw/deposit — is one persistent window.
    banking = s.bank_open or s.at_bank or action in (
        BFAction.GO_TO_BANK, BFAction.COLLECT_BARS, BFAction.REFILL_COFFER,
        BFAction.WITHDRAW_COAL, BFAction.WITHDRAW_ORE, BFAction.WITHDRAW_COINS,
        BFAction.FILL_COAL_BAG, BFAction.DEPOSIT_BARS)

    reg = _Reg()
    plan = plan or []

    # PRIMARY (bright) = the next action — learned prediction preferred so it LEADS
    # you, else the state policy. ON-DECK (dim) = the step after, drawn early so the
    # guidance arrives BEFORE you reach the step instead of after it.
    primary = _resolve_primary(plan[0] if plan else None, guidance, s, banking)
    ondeck = plan[1] if len(plan) > 1 else None

    # Context: coal stays lit across the whole banking sequence. The ORE ghost only
    # shows when it's actually relevant — on the coal+ore/ore trip, or when depositing
    # bars — NOT during coal withdrawals mid-cycle (bars sitting in the dispenser).
    # Classify the trip with the bag treated as full (skips the fill-bag transient):
    # if the furnace still needs loose coal it's a coal trip -> coal only.
    if banking and bt is not None:
        ratio = bt.coal_per_bar
        if ratio <= 0:
            materials = [bt.ore_item_id]                  # iron: no coal
        else:
            probe = s.replace(coal_bag_full=True, coal_bag_has_coal=True)
            ore_trip = not _furnace_needs_loose_coal(probe, ratio)
            materials = [ids.ITEM_COAL]
            if ore_trip or s.inv_bars > 0:                # ore trip, or depositing bars
                materials.append(bt.ore_item_id)
        for mid in materials:
            reg.add(("bank", mid), _PRIO_CONTEXT,
                    _bank_dir(mid, COLOR_SECONDARY, _material_label(mid, bt), s, layout))

    _add_ondeck(reg, ondeck, s, layout, bt)   # dim look-ahead (drawn before you get there)
    _add_primary(reg, primary, s, layout, bt)  # bright next click

    # Deposit bars: highlight the bar whenever we're carrying bars in a banking
    # context — heading to the bank AND while the UI is open — so it's lit the whole
    # time you're about to deposit, not only on the closed approach.
    if bt is not None and s.inv_bars > 0 and banking:
        reg.add(("inv", bt.bar_item_id), _PRIO_PRIMARY,
                {"kind": "invItem", "id": bt.bar_item_id, "color": COLOR_PRIMARY, "label": "Deposit bars"})

    # Wrong items: an ore or bar of a DIFFERENT type than the current run (e.g.
    # adamantite ore while doing mithril) -> flag the inventory cell RED. Coal,
    # coins, stamina, tools are never flagged (only foreign ores/bars).
    if bt is not None and s.inv_slots:
        wrong = _ALL_ORE_BAR_IDS - {bt.ore_item_id, bt.bar_item_id}
        for slot, iid in s.inv_slots:
            if iid in wrong:
                reg.add(("wrongslot", slot), _PRIO_PRIMARY,
                        {"kind": "invSlot", "slot": slot, "color": COLOR_WRONG, "label": "wrong item"})

    # Coffer when low / critical.
    if s.coffer_critical or (s.coffer_low and s.holding_coins):
        for oid in ids.COFFER_IDS:
            reg.add(("obj", oid), _PRIO_PRIMARY,
                    {"kind": "object", "id": oid, "color": COLOR_COFFER, "label": "Coffer", "outline": True})

    # NOTE: the close button is NOT a persistent "leaving" highlight. Closing the
    # bank is an INTERMEDIATE step (close -> fill coal bag -> reopen); on the final
    # exit the user clicks the belt directly with the bank still open. So the close
    # highlight is emitted only when the learned plan predicts "Close" as the next /
    # on-deck action (handled by _add_primary/_add_ondeck), never as an always-on box.

    emitted = reg.emit()
    cur_stage = _apply_stage_labels(emitted, guidance, bt)  # tag indicators, get current
    directives.extend(emitted)
    hud = _hud_lines(s)
    if cur_stage is not None:
        hud.insert(0, f"◆ Stage {cur_stage[0]}: {cur_stage[1]}")
    directives.append({"kind": "text", "anchor": "topRight", "lines": hud})
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


_LEARN_OBJ = {"belt": ObjTarget.CONVEYOR, "dispenser": ObjTarget.DISPENSER,
              "bankchest": ObjTarget.BANK_CHEST}

_PRIO_CONTEXT, _PRIO_PRIMARY = 1, 2


class _Reg:
    """Highlight registry: one box per target, higher priority wins. Kills the
    competing/overlapping boxes (learned vs material vs guidance on one slot)."""
    def __init__(self) -> None:
        self._m: Dict[Any, Any] = {}

    def add(self, key: Any, prio: int, directive: Optional[Dict[str, Any]]) -> None:
        if directive is None:
            return
        cur = self._m.get(key)
        if cur is None or prio > cur[0]:
            self._m[key] = (prio, directive)

    def emit(self) -> List[Dict[str, Any]]:
        return [d for _, d in self._m.values()]


def _bank_dir(item_id: int, color: str, label: Optional[str], s: BFStateSnapshot,
              layout: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Live bank item when open; cached ghost when closed; nothing if closed and
    position unknown (never a live bankItem the bridge can't place)."""
    if s.bank_open:
        return {"kind": "bankItem", "id": item_id, "color": color, "label": label}
    b = _bank_bounds(item_id, layout)
    if b is not None:
        return {"kind": "bankItemPredicted", "id": item_id, "x": b["x"], "y": b["y"],
                "color": color, "label": label}
    return None


def _valid_primary(tgt: Dict[str, Any], s: BFStateSnapshot, banking: bool) -> bool:
    k = tgt.get("kind")
    if k == "bankItem":
        return banking
    if k == "close":
        return banking                   # closing is an intermediate banking sub-step
    if k == "bankchest":
        return not s.bank_open           # opening the chest only when it's closed
    if k in ("belt", "dispenser"):
        return True                      # clickable even with the bank UI open (belt-exit)
    if k == "invItem":
        return True                      # inventory always visible
    return False


def _resolve_primary(learned_target: Optional[Dict[str, Any]], guidance: BFGuidance,
                     s: BFStateSnapshot, banking: bool) -> Optional[Dict[str, Any]]:
    """The single next-click target: learned prediction (what you actually do next)
    when it fits the context, else the state policy's target."""
    if learned_target and _valid_primary(learned_target, s, banking):
        return learned_target
    if guidance.bank_item_id >= 0:
        return {"kind": "bankItem", "id": guidance.bank_item_id}
    if guidance.inv_item_id >= 0:
        return {"kind": "invItem", "id": guidance.inv_item_id}
    ot = guidance.action.object_target
    for k, o in _LEARN_OBJ.items():
        if o == ot:
            return {"kind": k}
    return None


def _add_primary(reg: _Reg, primary: Optional[Dict[str, Any]], s: BFStateSnapshot,
                 layout: Dict[str, Any], bt: Optional[BarType]) -> None:
    if not primary:
        return
    k = primary.get("kind")
    if k == "bankItem":
        reg.add(("bank", primary["id"]), _PRIO_PRIMARY,
                _bank_dir(primary["id"], COLOR_PRIMARY, _material_label(primary["id"], bt), s, layout))
    elif k == "invItem":
        reg.add(("inv", primary["id"]), _PRIO_PRIMARY,
                {"kind": "invItem", "id": primary["id"], "color": COLOR_PRIMARY, "label": "Next"})
    elif k in _LEARN_OBJ:
        ot = _LEARN_OBJ[k]
        for oid in _OBJ_IDS.get(ot, []):
            reg.add(("obj", oid), _PRIO_PRIMARY,
                    {"kind": "object", "id": oid, "color": COLOR_OBJECT, "label": "Next", "outline": True})
        loc = _object_loc(ot, layout)
        if loc is not None:
            reg.add(("arrow", loc["x"], loc["y"]), _PRIO_PRIMARY,
                    {"kind": "worldArrow", "plane": loc.get("plane", 0),
                     "x": loc["x"], "y": loc["y"], "color": COLOR_OBJECT})
        hs = (layout.get("hotspots") or {}).get(str(_TARGET_HOTSPOT_ID.get(ot)))
        if hs is not None:
            reg.add(("tile", hs["x"], hs["y"]), _PRIO_PRIMARY,
                    {"kind": "tile", "plane": hs.get("plane", 0), "x": hs["x"], "y": hs["y"],
                     "color": COLOR_TILE, "fill": "#33ffcc00", "label": "Next"})
    elif k == "close":
        cb = (layout.get("widgets") or {}).get("bankClose")
        if cb is not None:
            reg.add(("close",), _PRIO_PRIMARY,
                    {"kind": "widgetPredicted", "group": ids.BANK_GROUP_ID, "child": cb.get("child", -1),
                     "x": cb["x"], "y": cb["y"], "color": COLOR_CLOSE, "label": "Close bank"})


def _add_ondeck(reg: _Reg, ondeck: Optional[Dict[str, Any]], s: BFStateSnapshot,
                layout: Dict[str, Any], bt: Optional[BarType]) -> None:
    """The step-after-next, dim, drawn AHEAD of time — only kinds that can render
    before you arrive (bank ghost, standing tile, world arrow, close)."""
    if not ondeck:
        return
    k = ondeck.get("kind")
    if k == "bankItem":
        reg.add(("bank", ondeck["id"]), _PRIO_CONTEXT,
                _bank_dir(ondeck["id"], COLOR_PREDICT_2, _material_label(ondeck["id"], bt), s, layout))
    elif k == "invItem":
        reg.add(("inv", ondeck["id"]), _PRIO_CONTEXT,
                {"kind": "invItem", "id": ondeck["id"], "color": COLOR_SECONDARY, "label": "soon"})
    elif k in _LEARN_OBJ:
        ot = _LEARN_OBJ[k]
        loc = _object_loc(ot, layout)
        if loc is not None:
            reg.add(("arrow", loc["x"], loc["y"]), _PRIO_CONTEXT,
                    {"kind": "worldArrow", "plane": loc.get("plane", 0),
                     "x": loc["x"], "y": loc["y"], "color": COLOR_SECONDARY})
        hs = (layout.get("hotspots") or {}).get(str(_TARGET_HOTSPOT_ID.get(ot)))
        if hs is not None:
            reg.add(("tile", hs["x"], hs["y"]), _PRIO_CONTEXT,
                    {"kind": "tile", "plane": hs.get("plane", 0), "x": hs["x"], "y": hs["y"],
                     "color": COLOR_SECONDARY, "label": "soon"})
    elif k == "close":
        cb = (layout.get("widgets") or {}).get("bankClose")
        if cb is not None:
            reg.add(("close",), _PRIO_CONTEXT,
                    {"kind": "widgetPredicted", "group": ids.BANK_GROUP_ID, "child": cb.get("child", -1),
                     "x": cb["x"], "y": cb["y"], "color": COLOR_CLOSE, "label": "Close soon"})


# ── Numbered rotation stages ─────────────────────────────────────────────────
# A stable number per action so the user can pinpoint "what should show at which
# stage vs later stages". Each on-screen indicator is tagged with the stage it
# belongs to; the HUD shows the current stage.
_STAGE = {
    BFAction.COLLECT_BARS: 1,
    BFAction.GO_TO_BANK: 2,
    BFAction.DEPOSIT_BARS: 3,
    BFAction.WITHDRAW_COAL: 4,
    BFAction.FILL_COAL_BAG: 5,
    BFAction.GO_TO_BELT: 6,
    BFAction.DEPOSIT_COAL: 7,
    BFAction.EMPTY_COAL_BAG: 8,
    BFAction.WITHDRAW_ORE: 9,
    BFAction.DEPOSIT_ORE: 10,
    BFAction.REFILL_COFFER: 11,
    BFAction.WITHDRAW_COINS: 12,
}
_ITEM_COAL_BAG_IDS = (ids.ITEM_COAL_BAG, 24480)  # closed + open coal bag


def _stage_action_for(d: Dict[str, Any], bt: Optional[BarType]) -> Optional[BFAction]:
    """Which rotation action an indicator represents (so it can be stage-tagged)."""
    k = d.get("kind")
    if k in ("bankItem", "bankItemPredicted", "invItem"):
        iid = d.get("id")
        if iid == ids.ITEM_COAL:
            return BFAction.WITHDRAW_COAL
        if bt is not None and iid == bt.ore_item_id:
            return BFAction.WITHDRAW_ORE
        if iid in _ITEM_COAL_BAG_IDS:
            return BFAction.FILL_COAL_BAG
        if bt is not None and iid == bt.bar_item_id:
            return BFAction.DEPOSIT_BARS
        if iid == ids.ITEM_COINS:
            return BFAction.WITHDRAW_COINS
    elif k == "object":
        oid = d.get("id")
        if oid == ids.CONVEYOR_BELT:
            return BFAction.GO_TO_BELT
        if oid in (ids.DISPENSER_FULL, ids.DISPENSER_COOLED, ids.DISPENSER_BASE):
            return BFAction.COLLECT_BARS
        if oid == ids.BANK_CHEST:
            return BFAction.GO_TO_BANK
        if oid in ids.COFFER_IDS:
            return BFAction.REFILL_COFFER
    elif k in ("widget", "widgetPredicted"):
        return BFAction.FILL_COAL_BAG          # bank close = part of the fill-bag step
    return None


def _apply_stage_labels(directives: List[Dict[str, Any]], guidance: BFGuidance,
                        bt: Optional[BarType]) -> Optional[tuple]:
    """Prefix each indicator's label with its stage number; return the current
    (number, label) for the HUD."""
    for d in directives:
        act = _stage_action_for(d, bt)
        n = _STAGE.get(act) if act is not None else None
        if n is not None:
            lbl = d.get("label")
            d["label"] = f"{n}. {lbl}" if lbl else f"Stage {n}"
    cur = _STAGE.get(guidance.action)
    return (cur, guidance.action.label) if cur is not None else None


def _material_label(item_id: int, bt: Optional[BarType]) -> Optional[str]:
    if item_id == ids.ITEM_COAL:
        return "Coal"
    if bt is not None and item_id == bt.ore_item_id:
        return f"{bt.display_name} ore"
    return None


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


def _detect_bar_type(inv: List[Dict[str, Any]],
                     varbits: Optional[Dict[int, int]] = None) -> Optional[BarType]:
    varbits = varbits or {}
    # ACTIVE ORE first — what you are smelting right NOW (inventory being withdrawn,
    # or ore loaded in the furnace). This must outrank leftover BARS from a previous
    # ore (e.g. uncollected adamantite bars) so switching to mithril takes effect.
    for bt in (BarType.MITHRIL, BarType.ADAMANTITE, BarType.RUNITE):
        if _count_item(inv, bt.ore_item_id) > 0 or varbits.get(bt.furnace_ore_varbit, 0) > 0:
            return bt
    # Then carried / queued BARS (weaker: can be stale output).
    for bt in (BarType.MITHRIL, BarType.ADAMANTITE, BarType.RUNITE):
        if _count_item(inv, bt.bar_item_id) > 0 or varbits.get(bt.furnace_bar_varbit, 0) > 0:
            return bt
    # Iron / steel.
    if _count_item(inv, ids.ITEM_IRON_ORE) > 0:
        return BarType.STEEL if _count_item(inv, ids.ITEM_COAL) > 0 else BarType.IRON
    if varbits.get(ids.VAR_FURNACE_STEEL_BARS, 0) > 0:
        return BarType.STEEL
    if varbits.get(ids.VAR_FURNACE_IRON_BARS, 0) > 0:
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
    if cfg != "AUTO":
        bt = _BAR_CONFIG.get(cfg)
    else:
        # live detection (inventory + furnace) wins; the remembered type only fills
        # the rare tick with nothing in inventory AND an empty furnace.
        bt = _detect_bar_type(inv, varbits) or _BAR_CONFIG.get((ctx.get("last_bar_type") or "").upper())

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
    # -1 = unknown (after reconnect): err coal-first -> treat as NOT full and NOT
    # carrying coal, so guidance routes to the bank rather than assuming a loaded bag
    # and heading to the belt (which suppressed the bank ghost). (12020 was GEM_BAG.)
    coal_bag_full = count >= ids.COAL_BAG_CAPACITY
    coal_bag_has_coal = count > 0

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
        inv_slots=tuple((i.get("slot"), i.get("id")) for i in inv if (i.get("id") or 0) > 0),
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
