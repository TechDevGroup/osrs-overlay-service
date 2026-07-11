"""Learned next-action model — derives what to highlight from the user's OWN
recorded interactions (actions.log), not hardcoded step assertions.

We canonicalize each menu click into an action token (option:target), learn an
order-2 transition model (backoff to order-1) over the token stream, and at
runtime predict the most likely next action(s) given the recent history. Each
token maps to a concrete highlight target (bank item / inv item / object / tile /
widget) which the policy renders with its existing live-or-ghost logic.

The model rebuilds from the log (and can be updated live as events arrive), so it
tracks the player's actual rotation and adapts if they change it — the highlight
is "what you actually do next after this", learned from you.
"""
from __future__ import annotations

import json
import re
import collections
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import ids

# Menu options that are movement / UI noise, not part of the task rotation.
_NOISE_OPTIONS = {
    "Walk here", "Select", "Inventory", "Skills", "Cancel", "View tab",
    "Logout", "Examine", "Grouping",
}


def canonical_token(event: Dict[str, Any]) -> Optional[str]:
    """menuOptionClicked -> 'Option:Target' token, or None for noise."""
    if event.get("name") != "menuOptionClicked":
        return None
    option = (event.get("option") or "").strip()
    if not option or option in _NOISE_OPTIONS:
        return None
    target = re.sub(r"<[^>]*>", "", event.get("target") or "").strip()  # strip color tags
    return f"{option}:{target}"


class ActionModel:
    def __init__(self, log_path: Optional[Path] = None):
        self.log_path = Path(log_path) if log_path else None
        self.o1: Dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
        self.o2: Dict[Tuple[str, str], collections.Counter] = collections.defaultdict(collections.Counter)
        self._prev2: Tuple[Optional[str], Optional[str]] = (None, None)
        self.count = 0
        if self.log_path and self.log_path.exists():
            self.rebuild()

    # ── learning ────────────────────────────────────────────────────────────
    def _learn_pair(self, a: Optional[str], b: str) -> None:
        if a is not None:
            self.o1[a][b] += 1
        p2, p1 = self._prev2
        if p1 is not None and p2 is not None:
            self.o2[(p2, p1)][b] += 1

    def observe(self, token: str) -> None:
        """Feed one new action token (live), updating the model + history."""
        p2, p1 = self._prev2
        self._learn_pair(p1, token)
        self._prev2 = (p1, token)
        self.count += 1

    def rebuild(self) -> None:
        """(Re)build the model from the whole log."""
        self.o1.clear(); self.o2.clear(); self._prev2 = (None, None); self.count = 0
        if not (self.log_path and self.log_path.exists()):
            return
        with open(self.log_path, "r", encoding="utf-8") as f:
            for ln in f:
                try:
                    e = json.loads(ln)
                except ValueError:
                    continue
                t = canonical_token(e)
                if t:
                    self.observe(t)

    # ── prediction ──────────────────────────────────────────────────────────
    def predict(self, history: Tuple[Optional[str], ...],
                accept: Optional[Callable[[str], bool]] = None,
                k: int = 3) -> List[Tuple[str, float]]:
        """Ranked (token, prob) for the next action given recent history
        (…, prev2, prev1). Order-2 first, backoff to order-1. `accept` filters to
        tokens that are actionable in the current state."""
        prev1 = history[-1] if history else None
        prev2 = history[-2] if len(history) >= 2 else None
        counter = None
        if prev2 is not None and prev1 is not None and (prev2, prev1) in self.o2:
            counter = self.o2[(prev2, prev1)]
        elif prev1 is not None and prev1 in self.o1:
            counter = self.o1[prev1]
        if not counter:
            return []
        total = sum(counter.values())
        ranked = [(t, n / total) for t, n in counter.most_common()]
        if accept is not None:
            ranked = [(t, p) for t, p in ranked if accept(t)]
        return ranked[:k]

    def plan(self, history: Tuple[Optional[str], ...], steps: int = 3) -> List[str]:
        """Greedy rollout of the next `steps` actions, so highlights can LEAD the
        player (show the next step, and the one after) instead of reacting to the
        step just taken. Stops early if the model has no confident continuation."""
        hist = [h for h in history if h is not None]
        out: List[str] = []
        for _ in range(steps):
            preds = self.predict(tuple(hist))
            if not preds:
                break
            nxt = preds[0][0]
            out.append(nxt)
            hist.append(nxt)
            # avoid a self-loop swallowing the plan (e.g. repeated Put-ore-on)
            if len(out) >= 2 and out[-1] == out[-2]:
                break
        return out


# ── token -> highlight target ────────────────────────────────────────────────
# Each token resolves to a semantic target the policy already knows how to draw.
# kinds: bankItem | invItem | object | dispenser | belt | bankchest | close | none
def token_target(token: str, bar_type) -> Optional[Dict[str, Any]]:
    if not token:
        return None
    option, _, target = token.partition(":")
    t = target.lower()
    ore_id = bar_type.ore_item_id if bar_type else -1
    bar_id = bar_type.bar_item_id if bar_type else -1
    if option in ("Withdraw-All", "Withdraw", "Withdraw-X", "Withdraw-All-but-1"):
        if "coal" in t:
            return {"kind": "bankItem", "id": ids.ITEM_COAL}
        if "ore" in t and ore_id > 0:
            return {"kind": "bankItem", "id": ore_id}
    if option in ("Fill", "Empty") and "coal bag" in t:
        return {"kind": "invItem", "id": ids.ITEM_COAL_BAG}
    if option == "Deposit-All" and "bar" in t and bar_id > 0:
        return {"kind": "invItem", "id": bar_id}
    if option == "Deposit-All" and "ore" in t and ore_id > 0:
        return {"kind": "invItem", "id": ore_id}
    if "conveyor belt" in t:
        return {"kind": "belt"}
    if "bar dispenser" in t:
        return {"kind": "dispenser"}
    if "bank chest" in t:
        return {"kind": "bankchest"}
    if option == "Close":
        return {"kind": "close"}
    return None
