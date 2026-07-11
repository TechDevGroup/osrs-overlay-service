"""Item / object / varbit ids for the Blast Furnace.

Ported 1:1 from the Java reference BFConstants.java in
TechDevGroup/runelite-blast-furnace-helper (BSD-2-Clause sources credited there:
RuneLite gameval ObjectID/VarbitID/ItemID + OSRS Wiki "Blast Furnace").

Cross-checked against the task spec:
  coal 453, adamantite ore 449, coal bag 12019, bank chest 26707,
  conveyor belt 9100, bar dispenser 9095/9096, coffer objects 29328/29329/29330,
  coffer varbit 5357, bar-dispenser (per-type bar) varbits 942-946.
"""

# ── Item ids ────────────────────────────────────────────────────────────────
ITEM_COAL = 453
ITEM_COAL_BAG = 12019
ITEM_COAL_BAG_FULL = 12020
ITEM_IRON_ORE = 440
ITEM_MITHRIL_ORE = 447
ITEM_ADAMANTITE_ORE = 449
ITEM_RUNITE_ORE = 451
ITEM_IRON_BAR = 2351
ITEM_STEEL_BAR = 2353
ITEM_MITHRIL_BAR = 2359
ITEM_ADAMANTITE_BAR = 2361
ITEM_RUNITE_BAR = 2363
ITEM_COINS = 995

# ── Game objects ────────────────────────────────────────────────────────────
CONVEYOR_BELT = 9100
BANK_CHEST = 26707

DISPENSER_BASE = 9092  # BLAST_FURNACE_DISPENSER
DISPENSER_EMPTY = 9093
DISPENSER_FORANIM = 9094
DISPENSER_FULL = 9095  # (Take)
DISPENSER_COOLED = 9096  # (Take/Check)
DISPENSER_IDS = (
    DISPENSER_BASE,
    DISPENSER_EMPTY,
    DISPENSER_FORANIM,
    DISPENSER_FULL,
    DISPENSER_COOLED,
)

# Coffer objects
COFFER_EMPTY = 29328
COFFER_FULL = 29329
COFFER_ACTIVE = 29330
COFFER_IDS = (COFFER_EMPTY, COFFER_FULL, COFFER_ACTIVE)

# ── Varbits ─────────────────────────────────────────────────────────────────
VAR_FURNACE_COAL = 949
VAR_FURNACE_IRON_ORE = 951
VAR_FURNACE_MITHRIL_ORE = 952
VAR_FURNACE_ADAMANTITE_ORE = 953
VAR_FURNACE_RUNITE_ORE = 954

# Per-type finished-bar counts waiting in the dispenser (942-946).
VAR_FURNACE_IRON_BARS = 942
VAR_FURNACE_STEEL_BARS = 943
VAR_FURNACE_MITHRIL_BARS = 944
VAR_FURNACE_ADAMANTITE_BARS = 945
VAR_FURNACE_RUNITE_BARS = 946

VAR_DISPENSER_STATE = 936
VAR_COFFER = 5357

# ── Capacities / loads (OSRS Wiki) ──────────────────────────────────────────
COAL_BAG_CAPACITY = 27
COAL_INV_LOAD = 27
ORE_LOAD = 27

# ── Coffer drain (OSRS Wiki "Blast Furnace") ────────────────────────────────
COFFER_DRAIN_PER_HOUR = 72_000
COFFER_DRAIN_PER_MINUTE = 1_200
COFFER_MAX = 20_000_000

# ── Widgets ─────────────────────────────────────────────────────────────────
# Bank close button — DISCOVERED as widget 12.2 (id 786434). Use the fixed id
# directly; the "Close" action-scan drifts to other widgets (scrollbar, etc.).
BANK_GROUP_ID = 12
BANK_CLOSE_CHILD = 2


def is_dispenser_object(obj_id: int) -> bool:
    return obj_id in DISPENSER_IDS


def is_coffer_object(obj_id: int) -> bool:
    return obj_id in COFFER_IDS
