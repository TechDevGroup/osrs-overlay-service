import json
from pathlib import Path

from service.action_model import ActionModel, canonical_token, token_target
from service.policy_bf import BarType


def _ev(option, target, name="menuOptionClicked"):
    return {"name": name, "option": option, "target": target}


def test_canonical_token_and_noise():
    assert canonical_token(_ev("Withdraw-All", "Coal")) == "Withdraw-All:Coal"
    # color tags stripped
    assert canonical_token(_ev("Take", "<col=00ffff>Bar dispenser</col>")) == "Take:Bar dispenser"
    # noise dropped
    assert canonical_token(_ev("Walk here", "")) is None
    assert canonical_token(_ev("Skills", "")) is None
    # non-click events ignored
    assert canonical_token({"name": "animationChanged"}) is None


def test_token_target_mapping():
    bt = BarType.ADAMANTITE
    assert token_target("Withdraw-All:Coal", bt) == {"kind": "bankItem", "id": 453}
    assert token_target("Withdraw-All:Adamantite ore", bt)["id"] == bt.ore_item_id
    assert token_target("Fill:Open coal bag", bt) == {"kind": "invItem", "id": 12019}
    assert token_target("Deposit-All:Adamantite bar", bt)["id"] == bt.bar_item_id
    assert token_target("Put-ore-on:Conveyor belt", bt) == {"kind": "belt"}
    assert token_target("Take:Bar dispenser", bt) == {"kind": "dispenser"}
    assert token_target("Use:Bank chest", bt) == {"kind": "bankchest"}
    assert token_target("Close:", bt) == {"kind": "close"}


def test_learns_and_predicts_a_cycle(tmp_path):
    # a repeating rotation -> the model should predict the next step deterministically
    cycle = ["Use:Bank chest", "Deposit-All:Adamantite bar", "Withdraw-All:Coal",
             "Close:", "Fill:Open coal bag", "Use:Bank chest", "Withdraw-All:Coal",
             "Put-ore-on:Conveyor belt", "Empty:Open coal bag", "Take:Bar dispenser"]
    log = tmp_path / "actions.log"
    with open(log, "w") as f:
        for _ in range(20):
            for tok in cycle:
                opt, _, tgt = tok.partition(":")
                f.write(json.dumps(_ev(opt, tgt)) + "\n")
    m = ActionModel(log)
    assert m.count > 100
    # order-2: after (Close:, Fill) you Use the bank chest
    preds = m.predict(("Close:", "Fill:Open coal bag"))
    assert preds and preds[0][0] == "Use:Bank chest"
    # after depositing bars you withdraw coal
    assert m.predict(("Use:Bank chest", "Deposit-All:Adamantite bar"))[0][0] == "Withdraw-All:Coal"


def test_predicts_against_real_log_if_present():
    real = Path.home() / ".runelite" / "overlay-service" / "actions.log"
    if not real.exists():
        return  # only runs on the dev host with a captured log
    m = ActionModel(real)
    # rebuild the token stream to score next-action accuracy
    toks = []
    with open(real) as f:
        for ln in f:
            try:
                t = canonical_token(json.loads(ln))
            except ValueError:
                continue
            if t:
                toks.append(t)
    ok = tot = 0
    for i in range(2, len(toks)):
        p = m.predict((toks[i - 2], toks[i - 1]))
        if p:
            ok += (p[0][0] == toks[i]); tot += 1
    # the rotation is highly repetitive; order-2 should beat 60%
    assert tot > 50 and ok / tot > 0.6


def test_no_duplicate_boxes_and_lookahead():
    from service.policy_bf import BFStateSnapshot, BarType, build_directives, derive
    from service.action_model import token_target
    L = {"bankItems": {"453": {"x": 530, "y": 151}, "449": {"x": 578, "y": 151}},
         "widgets": {"bankClose": {"x": 700, "y": 80, "child": 786434}}, "hotspots": {}}
    s = BFStateSnapshot(bar_type=BarType.ADAMANTITE, bank_open=True, inv_coal=0, furnace_coal=2)
    plan = [token_target("Withdraw-All:Coal", BarType.ADAMANTITE),
            token_target("Fill:Open coal bag", BarType.ADAMANTITE)]
    ds = build_directives(s, derive(s), L, plan=plan)
    # at most one highlight per bank slot / inv item (no competing boxes)
    seen = {}
    for d in ds:
        key = (d["kind"] in ("bankItem", "bankItemPredicted") and ("bank", d.get("id"))) or \
              (d["kind"] == "invItem" and ("inv", d.get("id")))
        if key:
            assert key not in seen, f"duplicate box for {key}"
            seen[key] = 1
    # look-ahead: the on-deck (fill bag) is shown while coal is the primary
    assert any(d["kind"] == "invItem" and d["id"] == 12019 for d in ds)
    # close is shown during the withdraw-coal -> fill-bag series (bag not full), one box
    assert sum(1 for d in ds if d["kind"] == "widgetPredicted") == 1
    # ...and NOT once the bag's full on the ore trip (belt time, not close time)
    ore = s.replace(coal_bag_full=True, coal_bag_has_coal=True, furnace_coal=90, inv_coal=0)
    assert not any(d["kind"] == "widgetPredicted" for d in build_directives(ore, derive(ore), L))
