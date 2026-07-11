"""Policy-port tests: feed representative BF state snapshots and assert the
derived next-action / directives match the tuned Java policy."""
import pytest

from service import ids
from service.policy_bf import (
    BarType, BFAction, BFStateSnapshot, derive, build_directives, build_snapshot,
)


def kinds(directives):
    return [d["kind"] for d in directives]


def bank_item_ids(directives):
    return [d["id"] for d in directives if d["kind"] in ("bankItem", "bankItemPredicted")]


# ── Coal-threshold split (the core tuned behaviour) ──────────────────────────
def test_adamantite_coal_trip_fcoal_low_grabs_full_coal():
    """fcoal=2, bag full, no loose coal in hand -> withdraw a full coal load."""
    s = BFStateSnapshot(
        bar_type=BarType.ADAMANTITE, bank_open=True,
        coal_bag_full=True, coal_bag_has_coal=True,
        inv_coal=0, inv_ore=0, furnace_coal=2, furnace_ore=0,
    )
    g = derive(s)
    assert g.action == BFAction.WITHDRAW_COAL
    assert g.bank_item_id == ids.ITEM_COAL


def test_adamantite_ore_trip_fcoal_high_proceeds_to_ore():
    """fcoal=56, bag full -> furnace covered by bag, go straight to ore 449."""
    s = BFStateSnapshot(
        bar_type=BarType.ADAMANTITE, bank_open=True,
        coal_bag_full=True, coal_bag_has_coal=True,
        inv_coal=0, inv_ore=0, furnace_coal=56, furnace_ore=0,
    )
    g = derive(s)
    assert g.action == BFAction.WITHDRAW_ORE
    assert g.bank_item_id == ids.ITEM_ADAMANTITE_ORE == 449


def test_switch_point_is_fcoal_54_for_adamantite():
    base = dict(bar_type=BarType.ADAMANTITE, bank_open=True,
                coal_bag_full=True, coal_bag_has_coal=True, inv_coal=0, inv_ore=0, furnace_ore=0)
    # fcoal=53 -> 53+27=80 < 81 -> still a coal trip.
    assert derive(BFStateSnapshot(furnace_coal=53, **base)).action == BFAction.WITHDRAW_COAL
    # fcoal=54 -> 54+27=81 < 81 is False -> ore trip.
    assert derive(BFStateSnapshot(furnace_coal=54, **base)).action == BFAction.WITHDRAW_ORE


# ── The coal+ore highlight bug: BOTH must light in their phases ──────────────
def test_adamantite_coal_phase_also_highlights_ore():
    """Live bug: only coal highlighted. Ported policy must light coal AND ore 449."""
    s = BFStateSnapshot(
        bar_type=BarType.ADAMANTITE, bank_open=True,
        coal_bag_full=True, coal_bag_has_coal=True,
        inv_coal=0, inv_ore=0, furnace_coal=2, furnace_ore=0,
    )
    g = derive(s)
    ds = build_directives(s, g)
    lit = bank_item_ids(ds)
    assert ids.ITEM_COAL in lit
    assert ids.ITEM_ADAMANTITE_ORE in lit  # 449 must appear in the coal+ore phase


def test_adamantite_ore_phase_also_highlights_coal():
    s = BFStateSnapshot(
        bar_type=BarType.ADAMANTITE, bank_open=True,
        coal_bag_full=True, coal_bag_has_coal=True,
        inv_coal=0, inv_ore=0, furnace_coal=56, furnace_ore=0,
    )
    g = derive(s)
    lit = bank_item_ids(build_directives(s, g))
    assert ids.ITEM_ADAMANTITE_ORE in lit
    assert ids.ITEM_COAL in lit


# ── Bank-acquire sequence (coal-before-ore) ──────────────────────────────────
def test_empty_bag_at_bank_withdraws_coal_first():
    s = BFStateSnapshot(bar_type=BarType.ADAMANTITE, bank_open=True,
                        coal_bag_full=False, coal_bag_has_coal=False, inv_coal=0)
    g = derive(s)
    assert g.action == BFAction.WITHDRAW_COAL
    assert g.bank_item_id == ids.ITEM_COAL


def test_coal_in_hand_fills_bag_before_ore():
    s = BFStateSnapshot(bar_type=BarType.ADAMANTITE, bank_open=True,
                        coal_bag_full=False, coal_bag_has_coal=False, inv_coal=27)
    g = derive(s)
    assert g.action == BFAction.FILL_COAL_BAG
    assert g.inv_item_id == ids.ITEM_COAL_BAG


def test_iron_uses_no_coal_goes_straight_to_ore():
    s = BFStateSnapshot(bar_type=BarType.IRON, bank_open=True, inv_ore=0)
    g = derive(s)
    assert g.action == BFAction.WITHDRAW_ORE
    assert g.bank_item_id == ids.ITEM_IRON_ORE


# ── Coffer priority ──────────────────────────────────────────────────────────
def test_coffer_critical_at_bank_withdraws_coins():
    s = BFStateSnapshot(bar_type=BarType.ADAMANTITE, bank_open=True, coffer_critical=True)
    g = derive(s)
    assert g.action == BFAction.WITHDRAW_COINS
    assert g.bank_item_id == ids.ITEM_COINS


def test_coffer_critical_holding_coins_refills():
    s = BFStateSnapshot(bar_type=BarType.ADAMANTITE, bank_open=False,
                        coffer_critical=True, holding_coins=True)
    assert derive(s).action == BFAction.REFILL_COFFER


# ── Bars in inventory bank them ──────────────────────────────────────────────
def test_inv_bars_go_to_bank_when_closed():
    s = BFStateSnapshot(bar_type=BarType.ADAMANTITE, bank_open=False, inv_bars=28)
    assert derive(s).action == BFAction.GO_TO_BANK


def test_inv_bars_deposit_when_bank_open():
    s = BFStateSnapshot(bar_type=BarType.ADAMANTITE, bank_open=True, inv_bars=28)
    assert derive(s).action == BFAction.DEPOSIT_BARS


# ── Belt unload ──────────────────────────────────────────────────────────────
def test_at_belt_deposits_coal_first():
    s = BFStateSnapshot(bar_type=BarType.ADAMANTITE, at_belt=True, inv_coal=27, inv_ore=27)
    assert derive(s).action == BFAction.DEPOSIT_COAL


def test_at_belt_empties_bag_when_no_loose():
    s = BFStateSnapshot(bar_type=BarType.ADAMANTITE, at_belt=True,
                        inv_coal=0, inv_ore=0, coal_bag_has_coal=True, free_slots=5)
    g = derive(s)
    assert g.action == BFAction.EMPTY_COAL_BAG
    assert g.inv_item_id == ids.ITEM_COAL_BAG


# ── Return leg: collect bars before banking ──────────────────────────────────
def test_return_leg_collects_bars_before_bank():
    s = BFStateSnapshot(bar_type=BarType.ADAMANTITE, furnace_bars=28, free_slots=28)
    assert derive(s).action == BFAction.COLLECT_BARS


def test_collect_bars_gated_on_free_slots():
    s = BFStateSnapshot(bar_type=BarType.ADAMANTITE, furnace_bars=28, free_slots=0)
    # No free slots -> cannot collect -> fall through to go-to-bank.
    assert derive(s).action == BFAction.GO_TO_BANK


# ── Fresh-inventory-at-bank directive list (report scenario) ─────────────────
def test_fresh_inventory_at_bank_directive_list():
    s = BFStateSnapshot(
        bar_type=BarType.ADAMANTITE, bank_open=True,
        coal_bag_full=False, coal_bag_has_coal=False, inv_coal=0, inv_ore=0,
        coffer_balance=200_000, session_seconds=0.0,
    )
    g = derive(s)
    ds = build_directives(s, g)
    assert g.action == BFAction.WITHDRAW_COAL
    k = kinds(ds)
    assert "bankItem" in k       # coal (primary) + ore (secondary)
    assert "text" in k           # HUD panel
    assert ids.ITEM_COAL in bank_item_ids(ds)
    assert ids.ITEM_ADAMANTITE_ORE in bank_item_ids(ds)


# ── Snapshot adapter (raw wire -> snapshot) ──────────────────────────────────
def test_build_snapshot_auto_detects_adamantite():
    raw = {
        "player": {"x": 1948, "y": 4957, "plane": 0},
        "inv": [{"slot": 0, "id": ids.ITEM_ADAMANTITE_ORE, "qty": 10},
                {"slot": 1, "id": ids.ITEM_COAL_BAG, "qty": 1}],
        "varbits": {str(ids.VAR_FURNACE_COAL): 2, str(ids.VAR_COFFER): 100000},
        "bank": {"open": True, "items": []},
        "objects": [],
    }
    ctx = {"coal_bag_count": 27, "bar_type_config": "AUTO",
           "coffer_low_minutes": 20, "coffer_critical_gp": 0}
    s = build_snapshot(raw, ctx)
    assert s.bar_type == BarType.ADAMANTITE
    assert s.bank_open is True
    assert s.coal_bag_full is True
    assert s.furnace_coal == 2
    assert s.coffer_balance == 100000


def test_hud_reports_coffer_minutes():
    s = BFStateSnapshot(bar_type=BarType.ADAMANTITE, bank_open=True, coffer_balance=24000)
    ds = build_directives(s, derive(s))
    hud = next(d for d in ds if d["kind"] == "text")
    joined = " ".join(hud["lines"])
    assert "Coffer" in joined
    assert "20m" in joined  # 24000 / 1200 = 20 min


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
