from service.server import DirectiveSmoother


def bank(cid, color): return {"kind": "bankItemPredicted", "id": cid, "x": 1, "y": 1, "color": color}


def test_grace_persist_across_brief_gap():
    sm = DirectiveSmoother()
    t = 0
    def step(ds):
        nonlocal t; t += 1; return {tuple(sorted(d.items())) for d in sm.smooth(ds, t)}
    step([bank(453, "#a")])                 # coal up
    # coal vanishes for a few ticks -> still held (no blink-off) within grace
    for _ in range(DirectiveSmoother.GRACE):
        assert any(k for k in step([]) )    # still emitted
    # past grace -> gone
    assert step([]) == set()


def test_color_debounce_holds_style():
    sm = DirectiveSmoother()
    sm.smooth([bank(453, "#dim")], 1)
    # immediate restyle is held to the prior look (no rapid color churn)
    out = sm.smooth([bank(453, "#bright")], 2)
    assert out[0]["color"] == "#dim"
    # after COLOR_HOLD ticks the new style is accepted
    out = sm.smooth([bank(453, "#bright")], 5)
    assert out[0]["color"] == "#bright"


def test_hud_never_persists_stale():
    sm = DirectiveSmoother()
    sm.smooth([{"kind": "text", "anchor": "topRight", "lines": ["a"]}], 1)
    assert sm.smooth([], 2) == []   # HUD not held when absent
