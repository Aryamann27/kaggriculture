"""Kaggriculture submission entrypoint.

This strategy makes a cow-led livestock stack its main mid-season engine.
It first establishes a dedicated wheat patch, then caps the herd by both
that live wheat capacity and built structure slots.  Every purchased or
placed head is backed by several whole days of wheat, rather than a shared
minimum buffer, so expanding the herd cannot create a starvation cascade.
The early crop patch funds NE expansion; NE then supplies the additional
wheat, pastures, and workers needed for a larger herd.  Sheep and geese are
secondary occupants after cow capacity is funded.  Milk and wool are sold
only in small, high-price batches to avoid manufacturing a deep glut.

This is an integration test of two independently-validated strategies:
`livestock-stack`'s feed-safe animal ramp (all of the constants/logic
above, untouched) is kept as the base, and `town-oracle`'s observed-demand
module is grafted on, but scoped *only* to the cash-crop side (allocation
across CARROT/TOMATO/MELON and their sell timing/thresholds). Wheat
allocation, the feed-safety gates, and the animal-purchase logic are
deliberately left exactly as they were in `livestock-stack`, since that
strategy's robustness (zero escapes, low variance, won 100% of paired
Tier 2 seeds) is the reason it's the stronger of the two parents -- this
tests whether demand-awareness can improve its cash-crop engine without
touching what already works.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


MAX_HANDS = 11
SHED_CAPACITY = 100
FORCE_SELL_DAY = 27
LAST_HAND_HIRE_DAY = 28
FINAL_ACTION_DAY = 29
MAX_MARKET_ORDERS = 10
SAFETY_BUFFER_DAYS = 2

# A dedicated livestock stack takes 30% of every unlocked field.  The same
# number of wheat tiles is reserved for its eventual feed supply, leaving
# the remainder for cash crops.  This is intentionally not based on today's
# animal count: wheat is planted before the herd is allowed to grow.
LIVESTOCK_SLOT_RATIO = 0.30
MIN_WHEAT_TILES = 10
WHEAT_TILES_PER_ANIMAL = 1.0
# A crop must already be in the ground before it can underwrite any animal
# structure or purchase.  Wheat takes days to become useful, so merely
# having seed money is not a feed source.
WHEAT_BOOTSTRAP_TILES = 10
# Keep five complete feedings per committed head in the shed.  This covers
# movement, harvest/replant gaps, and the two-day escape window without
# relying on a same-day market rescue.
FEED_DAYS_BUFFER = 5
EMERGENCY_FEED_DAYS = 3
ANIMAL_CASH_RESERVE = 400
MAX_NEW_ANIMALS_PER_TURN = 2
FIRST_ANIMAL_BUY_DAY = 2
# Keep only two empty structures ahead of the committed herd.  Structures
# are free but consume the same productive tiles needed to finance feed.
STRUCTURE_HEADROOM = 2
# Start with six hands for crop establishment; add one per three committed
# animals, up to eleven hands, so feed/harvest/care work grows with the herd.
BASE_HANDS = 6
ANIMALS_PER_ADDITIONAL_HAND = 3

LAND_ORDER = ["NE", "SW", "SE"]
LAND_PRICES = {"NE": 1000, "SW": 2000, "SE": 4000}
LAND_CASH_RESERVE = 1200
EARLY_NE_UTILIZATION_RATIO = 0.60
EARLY_NE_CASH_RESERVE = 900
EARLY_NE_MIN_DAY = 1


@dataclass(frozen=True)
class CropSpec:
    seed_cost: int
    first_yield_day: int
    max_yield_day: int
    max_yield: int
    ongoing: bool
    base_price: int
    sell_threshold_ratio: float  # sell once price >= ratio * base_price
    allocation_ratio: float  # share of the *crop* tile budget


CROPS = {
    "WHEAT":      CropSpec(10, 2, 4, 6, False, base_price=25, sell_threshold_ratio=0.70, allocation_ratio=0.0),
    "CARROT":     CropSpec(20, 2, 3, 4, False, base_price=35, sell_threshold_ratio=0.75, allocation_ratio=0.45),
    "TOMATO":     CropSpec(50, 8, 8, 4, True, base_price=60, sell_threshold_ratio=0.70, allocation_ratio=0.35),
    "STRAWBERRY": CropSpec(100, 10, 10, 4, True, base_price=120, sell_threshold_ratio=0.75, allocation_ratio=0.0),
    "MELON":      CropSpec(80, 10, 12, 6, False, base_price=250, sell_threshold_ratio=0.85, allocation_ratio=0.20),
}

# Strawberry is deferred: same capital-intensive, glut-sensitive profile as
# tomato/melon without adding a distinct risk/return trade-off yet.
PLANTABLE_CROPS = ["WHEAT", "CARROT", "TOMATO", "MELON"]
CASH_CROPS = ["CARROT", "TOMATO", "MELON"]

# Plant only if there's enough season left for the crop to mature, produce,
# and be sold before the engine stops accepting new work.
LAST_PLANT_DAY = {
    crop: FINAL_ACTION_DAY
    - (spec.first_yield_day if spec.ongoing else spec.max_yield_day)
    - SAFETY_BUFFER_DAYS
    for crop, spec in CROPS.items()
}


@dataclass(frozen=True)
class AnimalSpec:
    cost: int
    structure: str  # "COOP" or "PASTURE"
    first_yield_day: int
    interval: int
    max_held: int
    base_price: int
    sell_threshold_ratio: float
    allocation_ratio: float  # share of the *livestock* tile budget


ANIMALS = {
    "COW":   AnimalSpec(400, "PASTURE", 8, 2, 6, base_price=160, sell_threshold_ratio=0.92, allocation_ratio=0.75),
    "SHEEP": AnimalSpec(500, "PASTURE", 6, 3, 6, base_price=200, sell_threshold_ratio=0.95, allocation_ratio=0.20),
    "GOOSE": AnimalSpec(300, "COOP", 4, 1, 4, base_price=50, sell_threshold_ratio=0.70, allocation_ratio=0.05),
}

# Priority order for allocation/tie-breaking: cow has the best $/day and the
# fastest capital payback, then sheep, then goose.
PLANTABLE_ANIMALS = ["COW", "SHEEP", "GOOSE"]
ANIMAL_PRODUCT = {"COW": "MILK", "SHEEP": "WOOL", "GOOSE": "EGG"}
STRUCTURE_KINDS = ["PASTURE", "COOP"]

# Buy only if there's enough season left for the animal to reach its first
# production before the engine stops accepting new work.
LAST_ANIMAL_BUY_DAY = {
    animal: FINAL_ACTION_DAY - spec.first_yield_day - SAFETY_BUFFER_DAYS
    for animal, spec in ANIMALS.items()
}
# A structure is only worth building while at least one animal that uses it
# could still be bought in time.
LAST_STRUCTURE_BUILD_DAY = {
    kind: max(
        LAST_ANIMAL_BUY_DAY[a] for a in PLANTABLE_ANIMALS if ANIMALS[a].structure == kind
    )
    for kind in STRUCTURE_KINDS
}

# Product name -> (base_price, sell_threshold_ratio), covering crop and
# animal products so `_market_orders` can sell everything with one loop.
SELL_SPECS: dict[str, tuple[int, float]] = {
    crop: (CROPS[crop].base_price, CROPS[crop].sell_threshold_ratio) for crop in PLANTABLE_CROPS
}
for _animal, _product in ANIMAL_PRODUCT.items():
    _spec = ANIMALS[_animal]
    SELL_SPECS[_product] = (_spec.base_price, _spec.sell_threshold_ratio)

# A single order can walk a glut-sensitive product's price down its steep
# curve in one shot.  Milk and wool wait for a worthwhile batch and then
# sell only a small tranche, allowing town demand to repair the price.
MAX_SELL_PER_TURN = {"MELON": 5, "MILK": 4, "WOOL": 3}
MIN_SELL_BATCH = {"MILK": 4, "WOOL": 3}

# --- Town demand module, ported from `strategy/town-oracle` -------------
# Scoped to the cash-crop side only: which of CARROT/TOMATO/MELON to grow,
# and when to sell them. Wheat's allocation and every animal-safety gate
# above are untouched.
CASH_CROP_PRODUCTS = {"CARROT", "TOMATO", "MELON"}
TOWN_SHOP_INTERVAL = 4
TOWN_CENTER_INTERVAL = 12
SHOP_PRODUCT_UNITS = {
    "BAKERY": {"EGG": 1, "WHEAT": 1},
    "PIZZA_SHOP": {"MILK": 1, "TOMATO": 1, "WHEAT": 1},
    "BRUNCH_SPOT": {"EGG": 1, "WHEAT": 1, "STRAWBERRY": 1},
    "YARN_STORE": {"WOOL": 2},
    "ICE_CREAM_SHOP": {"STRAWBERRY": 1, "MILK": 1, "WHEAT": 1},
    "PET_CAFE": {"CARROT": 2},
    "SMOOTHIE_SHOP": {"STRAWBERRY": 1, "MILK": 1},
    "FARMERS_MARKET": {"WHEAT": 1, "CARROT": 1, "TOMATO": 1, "STRAWBERRY": 1},
}


@dataclass(frozen=True)
class TownDemand:
    """Observed product demand, without predicting future shop unlocks."""

    score: dict[str, int]
    shop_units: dict[str, int]


def _town_demand(town: dict[str, Any] | None, day: int) -> TownDemand:
    """Score only currently-unlocked shops at their documented demand rate."""
    center_units = 4 if day >= 20 else 2 if day >= 10 else 1
    score = {product: center_units for product in SELL_SPECS}
    shop_units = {product: 0 for product in SELL_SPECS}
    unlocked = _as_dict(town).get("unlocked_shops", [])
    if not isinstance(unlocked, list):
        unlocked = []

    for shop_name in unlocked:
        products = SHOP_PRODUCT_UNITS.get(shop_name)
        if products is None:
            continue
        for product, units in products.items():
            if product not in score:
                continue
            shop_units[product] += units
            score[product] += 3 * units

    return TownDemand(score=score, shop_units=shop_units)


def _demand_adjusted_cash_crop_targets(budget: int, demand: TownDemand) -> dict[str, int]:
    """Split the cash-crop budget toward whichever crop known shops want,
    instead of `livestock-stack`'s fixed 45/35/20 ratio."""
    weights = {
        "CARROT": 2.2 + 0.35 * demand.score["CARROT"],
        "TOMATO": 2.5 + 0.35 * demand.score["TOMATO"],
        # No town shop consumes melons; keep a small diversification share.
        "MELON": 0.25,
    }
    total_weight = sum(weights.values())
    ratios = {crop: weight / total_weight for crop, weight in weights.items()}
    return _allocate_by_ratio(budget, CASH_CROPS, ratios)


def _post_town_drain_window(product: str, demand: TownDemand, hour: int) -> bool:
    """Sell just after the town has drained this product's inventory."""
    interval = TOWN_SHOP_INTERVAL if demand.shop_units.get(product, 0) else TOWN_CENTER_INTERVAL
    return hour % interval == 1


def _sell_threshold_ratio(product: str, threshold_ratio: float, demand: TownDemand) -> float:
    """Known demand permits a modestly earlier sale without betting on it."""
    discount = min(0.15, 0.025 * demand.shop_units.get(product, 0))
    return max(0.50, threshold_ratio - discount)


@dataclass(frozen=True)
class Task:
    priority: int
    position: tuple[int, int]
    action: list[str]


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _tile_at(farm: dict[str, Any], position: tuple[int, int]) -> Any:
    x, y = position
    return farm["tiles"][y][x]


def _manhattan(a: tuple[int, int], b: tuple[int, int]) -> int:
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def _move_toward(origin: tuple[int, int], destination: tuple[int, int]) -> list[str]:
    """Take a deterministic Manhattan step toward a target tile."""
    ox, oy = origin
    dx, dy = destination
    if ox < dx:
        return ["EAST"]
    if ox > dx:
        return ["WEST"]
    if oy < dy:
        return ["SOUTH"]
    if oy > dy:
        return ["NORTH"]
    return ["PASS"]


def _shed_access_tiles(board_size: int) -> list[tuple[int, int]]:
    """The four inner-corner tiles orthogonally adjacent to the shed."""
    half = board_size // 2
    return [(half - 1, half - 1), (half, half - 1), (half - 1, half), (half, half)]


def _is_shed_adjacent(position: tuple[int, int], board_size: int) -> bool:
    return position in set(_shed_access_tiles(board_size))


def _nearest_shed_tile(position: tuple[int, int], board_size: int) -> tuple[int, int]:
    return min(_shed_access_tiles(board_size), key=lambda t: _manhattan(position, t))


def _unlocked_positions(farm: dict[str, Any]) -> list[tuple[int, int]]:
    """Return usable farm tiles in stable near-shed order."""
    tiles = farm.get("tiles", [])
    if not tiles:
        return []

    board_size = len(tiles)
    shed_corner = (max(0, board_size // 2 - 1), max(0, board_size // 2 - 1))
    positions = [
        (x, y)
        for y, row in enumerate(tiles)
        for x, tile in enumerate(row)
        if tile != "LOCKED"
    ]
    return sorted(
        positions,
        key=lambda pos: (_manhattan(pos, shed_corner), pos[1], pos[0]),
    )


@dataclass(frozen=True)
class FieldScan:
    tasks: list[Task]
    crop_active: dict[str, int]
    crop_maturing: dict[str, int]
    occupied_by_animal: dict[str, int]
    structures_built: dict[str, int]
    empty: list[tuple[int, int]]
    weeds: list[tuple[int, int]]
    empty_structures: dict[str, list[tuple[int, int]]]
    unfed_animals: list[tuple[int, int]]
    fertilizable_wheat: list[tuple[int, int]]


def _scan_field(farm: dict[str, Any], day: int, positions: list[tuple[int, int]]) -> FieldScan:
    """One pass over the farm: care/harvest tasks, occupancy counts, and the
    empty/weed/logistics-relevant tiles needed to plan the rest of the turn.

    FEED/PLACE/FERTILIZE are deliberately *not* turned into `Task`s here --
    all three require the acting unit to already be carrying an item that
    only ever lands in the shed, which a single-position `Task` can't
    express. `_logistics_actions` handles those using the lists below.
    """
    tasks: list[Task] = []
    crop_active = {crop: 0 for crop in PLANTABLE_CROPS}
    crop_maturing = {crop: 0 for crop in PLANTABLE_CROPS}
    occupied_by_animal = {animal: 0 for animal in PLANTABLE_ANIMALS}
    structures_built = {kind: 0 for kind in STRUCTURE_KINDS}
    empty: list[tuple[int, int]] = []
    weeds: list[tuple[int, int]] = []
    empty_structures: dict[str, list[tuple[int, int]]] = {kind: [] for kind in STRUCTURE_KINDS}
    unfed_animals: list[tuple[int, int]] = []
    fertilizable_wheat: list[tuple[int, int]] = []

    for position in positions:
        tile = _tile_at(farm, position)

        if tile is None:
            empty.append(position)
            continue
        if not isinstance(tile, dict):
            continue

        kind = tile.get("kind")

        if kind == "WEED":
            weeds.append(position)
            continue

        if kind == "PLANT":
            crop = tile.get("crop")
            spec = CROPS.get(crop)
            if spec is None:
                continue
            age = day - int(tile.get("planted_day", day))
            watered = bool(tile.get("watered_today", False))
            yield_units = int(tile.get("yield_units", 0))

            if crop in crop_active:
                crop_active[crop] += 1
                if not spec.ongoing and age >= spec.max_yield_day:
                    crop_maturing[crop] += 1

            if (
                crop == "WHEAT"
                and yield_units < spec.max_yield
                and age < spec.max_yield_day
                and int(tile.get("fertilized_until_day", -1)) < day
            ):
                fertilizable_wheat.append(position)

            # Care comes first.  This avoids losing crops to the day refresh
            # and lets one-time crops receive their current-day yield bonus.
            needs_water = not watered and (spec.ongoing or age <= spec.max_yield_day)
            if needs_water:
                tasks.append(Task(100, position, ["WATER"]))

            mature = age >= spec.first_yield_day
            if spec.ongoing:
                can_harvest = yield_units > 0 and mature
            else:
                # Wait for the bonus window to close (or the hard cap to be
                # reached, like melon naturally does) instead of harvesting
                # the moment it's merely old enough -- harvesting right at
                # `first_yield_day` throws away every later bonus-window day.
                bonus_window_closed = age > spec.max_yield_day
                cap_reached = yield_units >= spec.max_yield
                can_harvest = yield_units > 0 and mature and (bonus_window_closed or cap_reached)
            if can_harvest:
                tasks.append(Task(90, position, ["HARVEST"]))
            continue

        if kind in STRUCTURE_KINDS:
            structures_built[kind] += 1
            animal = tile.get("animal")
            if not animal:
                empty_structures[kind].append(position)
                continue
            occupied_by_animal[animal] = occupied_by_animal.get(animal, 0) + 1
            if not tile.get("fed_today", False):
                unfed_animals.append(position)
            if tile.get("yield_units", 0) > 0:
                tasks.append(Task(90, position, ["HARVEST"]))
            if not tile.get("cared_today", False):
                tasks.append(Task(70, position, ["CARE"]))
            if tile.get("fertilizer_available", False):
                tasks.append(Task(60, position, ["COLLECT_FERTILIZER"]))
            continue

    return FieldScan(
        tasks=tasks,
        crop_active=crop_active,
        crop_maturing=crop_maturing,
        occupied_by_animal=occupied_by_animal,
        structures_built=structures_built,
        empty=empty,
        weeds=weeds,
        empty_structures=empty_structures,
        unfed_animals=unfed_animals,
        fertilizable_wheat=fertilizable_wheat,
    )


def _allocate_by_ratio(budget: int, keys: list[str], ratio: dict[str, float]) -> dict[str, int]:
    """Largest-remainder rounding of ratio shares of `budget` across `keys`."""
    raw = {k: budget * ratio[k] for k in keys}
    targets = {k: int(raw[k]) for k in keys}
    leftover = budget - sum(targets.values())
    order = sorted(keys, key=lambda k: raw[k] - targets[k], reverse=True)
    for k in order[: max(0, leftover)]:
        targets[k] += 1
    return targets


def _feed_supported_herd(wheat_tiles: int) -> int:
    """Return the herd size that live wheat can safely support.

    The bootstrap floor keeps empty or just-seeded fields from being treated
    as a feed source.  Animals can remain safely in the shed while a new
    wheat cycle is planted, but they cannot be bought or placed beyond this
    cap.
    """
    if wheat_tiles < WHEAT_BOOTSTRAP_TILES:
        return 0
    return math.floor(wheat_tiles / WHEAT_TILES_PER_ANIMAL)


@dataclass(frozen=True)
class FieldPlan:
    tasks: list[Task]
    field_capacity: int
    crop_active: dict[str, int]
    crop_maturing: dict[str, int]
    crop_targets: dict[str, int]
    animal_targets: dict[str, int]
    wheat_active: int
    feed_supported_herd: int
    occupied_by_animal: dict[str, int]
    structures_built: dict[str, int]
    empty_structures: dict[str, list[tuple[int, int]]]
    unfed_animals: list[tuple[int, int]]
    fertilizable_wheat: list[tuple[int, int]]
    demand: TownDemand


def _field_tasks(
    farm: dict[str, Any],
    private: dict[str, Any],
    day: int,
    town: dict[str, Any] | None = None,
) -> FieldPlan:
    """Build all current position-based unit tasks and the counts needed for
    market planning."""
    positions = _unlocked_positions(farm)
    field_capacity = len(positions)
    scan = _scan_field(farm, day, positions)
    demand = _town_demand(town, day)
    tasks = list(scan.tasks)

    livestock_budget = round(field_capacity * LIVESTOCK_SLOT_RATIO)
    wheat_target = max(
        MIN_WHEAT_TILES,
        math.ceil(livestock_budget * WHEAT_TILES_PER_ANIMAL),
    )
    cash_crop_budget = max(0, field_capacity - livestock_budget - wheat_target)
    crop_targets = {crop: 0 for crop in PLANTABLE_CROPS}
    crop_targets.update(_demand_adjusted_cash_crop_targets(cash_crop_budget, demand))
    crop_targets["WHEAT"] = wheat_target

    animal_targets = _allocate_by_ratio(
        livestock_budget,
        PLANTABLE_ANIMALS,
        {a: ANIMALS[a].allocation_ratio for a in PLANTABLE_ANIMALS},
    )
    wheat_active = scan.crop_active["WHEAT"]
    feed_supported_herd = _feed_supported_herd(wheat_active)
    shed = _as_dict(private.get("shed"))
    carried_animals = sum(
        max(0, int(_as_dict(inventory).get(animal, 0)))
        for inventory in private.get("inventories", [])
        if isinstance(inventory, dict)
        for animal in PLANTABLE_ANIMALS
    )
    committed_herd = (
        sum(scan.occupied_by_animal.values())
        + sum(max(0, int(shed.get(animal, 0))) for animal in PLANTABLE_ANIMALS)
        + carried_animals
    )
    # Structure construction follows established wheat, cow first.  Empty
    # structures consume land, so build just two slots beyond the committed
    # herd instead of filling the whole animal allocation before it is paid
    # for.  This preserves a cash-crop engine through the herd ramp.
    buildable_slots = min(
        livestock_budget,
        feed_supported_herd,
        committed_herd + STRUCTURE_HEADROOM,
    )
    buildable_animal_targets = {animal: 0 for animal in PLANTABLE_ANIMALS}
    for animal in PLANTABLE_ANIMALS:
        assigned = min(animal_targets[animal], buildable_slots)
        buildable_animal_targets[animal] = assigned
        buildable_slots -= assigned

    structure_targets = {kind: 0 for kind in STRUCTURE_KINDS}
    for animal in PLANTABLE_ANIMALS:
        structure_targets[ANIMALS[animal].structure] += buildable_animal_targets[animal]

    empty_structures = scan.empty_structures
    unfed_animals = scan.unfed_animals
    fertilizable_wheat = scan.fertilizable_wheat

    if day < FINAL_ACTION_DAY:
        # The engine stops before any turn after the last day's inventory
        # drop, so nothing newly planted/built/harvested on the final day
        # can ever be sold.  Stop generating new work at that point and let
        # the market orders spend the whole day liquidating what's already
        # in the shed from the prior day.
        tasks.extend(Task(50, position, ["DIG"]) for position in scan.weeds)

        seeds = _as_dict(private.get("seeds"))
        owned_seeds = {crop: max(0, int(seeds.get(crop, 0))) for crop in PLANTABLE_CROPS}
        crop_needs = {
            crop: max(0, crop_targets[crop] - (scan.crop_active[crop] - scan.crop_maturing[crop]))
            for crop in PLANTABLE_CROPS
        }
        structure_needs = {
            kind: max(0, structure_targets[kind] - scan.structures_built[kind])
            for kind in STRUCTURE_KINDS
        }

        for position in scan.empty:
            options: list[tuple[int, list[str]]] = []
            for crop in PLANTABLE_CROPS:
                if owned_seeds[crop] > 0 and crop_needs[crop] > 0 and day <= LAST_PLANT_DAY[crop]:
                    options.append((crop_needs[crop], ["PLANT", crop]))
            for kind in STRUCTURE_KINDS:
                if structure_needs[kind] > 0 and day <= LAST_STRUCTURE_BUILD_DAY[kind]:
                    options.append((structure_needs[kind], [f"BUILD_{kind}"]))
            if not options:
                continue
            # Prefer whichever option is furthest short of its target.
            _need, action = max(options, key=lambda o: o[0])
            tasks.append(Task(40, position, action))
            if action[0] == "PLANT":
                crop = action[1]
                owned_seeds[crop] -= 1
                crop_needs[crop] = max(0, crop_needs[crop] - 1)
            else:
                kind = action[0].split("_", 1)[1]
                structure_needs[kind] = max(0, structure_needs[kind] - 1)
    else:
        tasks = []
        empty_structures = {kind: [] for kind in STRUCTURE_KINDS}
        unfed_animals = []
        fertilizable_wheat = []

    return FieldPlan(
        tasks=tasks,
        field_capacity=field_capacity,
        crop_active=scan.crop_active,
        crop_maturing=scan.crop_maturing,
        crop_targets=crop_targets,
        animal_targets=animal_targets,
        wheat_active=wheat_active,
        feed_supported_herd=feed_supported_herd,
        occupied_by_animal=scan.occupied_by_animal,
        structures_built=scan.structures_built,
        empty_structures=empty_structures,
        unfed_animals=unfed_animals,
        fertilizable_wheat=fertilizable_wheat,
        demand=demand,
    )


def _animal_fill_priority(plan: FieldPlan) -> list[str]:
    """Animal types ordered by how far below their target population they
    are, most-needed first."""
    needs = {
        a: max(0, plan.animal_targets.get(a, 0) - plan.occupied_by_animal.get(a, 0))
        for a in PLANTABLE_ANIMALS
    }
    return sorted(PLANTABLE_ANIMALS, key=lambda a: (-needs[a], PLANTABLE_ANIMALS.index(a)))


def _logistics_actions(
    unit_positions: list[tuple[int, int]],
    private: dict[str, Any],
    board_size: int,
    plan: FieldPlan,
    animal_fill_priority: list[str],
) -> list[list[str] | None]:
    """Per-unit carry-then-act actions for animal delivery, feeding, and
    fertilizing.

    PLACE/FEED/FERTILIZE all require the acting unit to already be holding
    the relevant item, but bought animals, harvested wheat, and collected
    fertilizer only ever land in the shed.  This resolves, for each unit
    independently, whether it should keep carrying an item toward its
    destination, fetch one, or fall through to the normal position-based
    task pool (returned as `None`).
    """
    inventories = private.get("inventories")
    inventories = inventories if isinstance(inventories, list) else []
    shed = _as_dict(private.get("shed"))

    remaining_structures = {kind: list(positions) for kind, positions in plan.empty_structures.items()}
    remaining_unfed = list(plan.unfed_animals)
    remaining_fertilizable = list(plan.fertilizable_wheat)
    remaining_shed_animals = {a: int(shed.get(a, 0)) for a in PLANTABLE_ANIMALS}
    remaining_shed_wheat = int(shed.get("WHEAT", 0))
    remaining_shed_fertilizer = int(shed.get("FERTILIZER", 0))
    placed_animals = sum(plan.occupied_by_animal.values())
    # An animal in a unit inventory has already been committed to a
    # structure, even though the board has not reflected that placement yet.
    # Count it before scheduling new pickups so several units cannot consume
    # one herd-cap slot or one feed buffer at the same time.
    pending_placements = sum(
        int(_as_dict(inventories[idx]).get(animal, 0))
        for idx in range(len(inventories))
        for animal in PLANTABLE_ANIMALS
    )

    actions: list[list[str] | None] = []
    for idx, position in enumerate(unit_positions):
        inv = _as_dict(inventories[idx]) if idx < len(inventories) else {}

        carried_animal = next((a for a in PLANTABLE_ANIMALS if inv.get(a, 0) > 0), None)
        if carried_animal is not None:
            targets = remaining_structures.get(ANIMALS[carried_animal].structure, [])
            required_feed = (placed_animals + pending_placements) * FEED_DAYS_BUFFER
            if (
                not targets
                or placed_animals + pending_placements > plan.feed_supported_herd
                or remaining_shed_wheat < required_feed
            ):
                actions.append(None)
                continue
            target = min(targets, key=lambda p: _manhattan(position, p))
            # Claim the destination while the animal walks; otherwise two
            # carriers can converge on one empty structure in the same turn.
            targets.remove(target)
            if position == target:
                actions.append(["PLACE", carried_animal])
                placed_animals += 1
                pending_placements -= 1
            else:
                actions.append(_move_toward(position, target))
            continue

        if int(inv.get("WHEAT", 0)) > 0 and remaining_unfed:
            target = min(remaining_unfed, key=lambda p: _manhattan(position, p))
            if position == target:
                actions.append(["FEED"])
                remaining_unfed.remove(target)
            else:
                actions.append(_move_toward(position, target))
            continue

        if int(inv.get("FERTILIZER", 0)) > 0 and remaining_fertilizable:
            target = min(remaining_fertilizable, key=lambda p: _manhattan(position, p))
            if position == target:
                actions.append(["FERTILIZE"])
                remaining_fertilizable.remove(target)
            else:
                actions.append(_move_toward(position, target))
            continue

        animal_choice = next(
            (
                a
                for a in animal_fill_priority
                if remaining_shed_animals.get(a, 0) > 0
                and remaining_structures.get(ANIMALS[a].structure)
                and placed_animals + pending_placements < plan.feed_supported_herd
                # Reserve the full buffer for every existing and committed
                # head, not a reusable three-wheat minimum for the whole
                # herd.  If this is false the animal remains safely in the
                # shed until feed is genuinely available.
                and remaining_shed_wheat
                >= (placed_animals + pending_placements + 1) * FEED_DAYS_BUFFER
            ),
            None,
        )
        if animal_choice is not None:
            if _is_shed_adjacent(position, board_size):
                actions.append(["PICKUP", animal_choice, 1])
                remaining_shed_animals[animal_choice] -= 1
                # Reserve a structure slot now so other units picking up
                # this turn don't also claim it -- the animal being carried
                # can't be placed anywhere else.
                remaining_structures[ANIMALS[animal_choice].structure].pop()
                pending_placements += 1
            else:
                actions.append(_move_toward(position, _nearest_shed_tile(position, board_size)))
            continue

        if remaining_unfed and remaining_shed_wheat > 0:
            if _is_shed_adjacent(position, board_size):
                take = min(remaining_shed_wheat, len(remaining_unfed))
                actions.append(["PICKUP", "WHEAT", take])
                remaining_shed_wheat -= take
                # Claim these animals as this unit's to feed, so other idle
                # units this same turn don't also fetch wheat for them.
                del remaining_unfed[:take]
            else:
                actions.append(_move_toward(position, _nearest_shed_tile(position, board_size)))
            continue

        if remaining_fertilizable and remaining_shed_fertilizer > 0:
            if _is_shed_adjacent(position, board_size):
                take = min(remaining_shed_fertilizer, len(remaining_fertilizable))
                actions.append(["PICKUP", "FERTILIZER", take])
                remaining_shed_fertilizer -= take
                del remaining_fertilizable[:take]
            else:
                actions.append(_move_toward(position, _nearest_shed_tile(position, board_size)))
            continue

        actions.append(None)

    return actions


def _assign_actions(
    positions: list[tuple[int, int]],
    tasks: list[Task],
) -> list[list[str]]:
    """Give each unit a distinct high-priority target, then move or act."""
    available = list(tasks)
    actions: list[list[str]] = []

    for position in positions:
        if not available:
            actions.append(["PASS"])
            continue

        best_index = min(
            range(len(available)),
            key=lambda index: (
                -available[index].priority,
                _manhattan(position, available[index].position),
                available[index].position[1],
                available[index].position[0],
            ),
        )
        task = available.pop(best_index)
        if position == task.position:
            actions.append(task.action)
        else:
            actions.append(_move_toward(position, task.position))
    return actions


def _fib(n: int) -> int:
    first, second = 1, 1
    for _ in range(n):
        first, second = second, first + second
    return first


def _carried_animal_counts(private: dict[str, Any]) -> dict[str, int]:
    """Count animals already carried by field units."""
    counts = {animal: 0 for animal in PLANTABLE_ANIMALS}
    inventories = private.get("inventories")
    if not isinstance(inventories, list):
        return counts
    for inventory in inventories:
        inventory_dict = _as_dict(inventory)
        for animal in PLANTABLE_ANIMALS:
            counts[animal] += max(0, int(inventory_dict.get(animal, 0)))
    return counts


def _desired_hand_count(total_animals: int) -> int:
    """Scale daily staffing with feeding and livestock-care workload."""
    extra_hands = math.ceil(total_animals / ANIMALS_PER_ADDITIONAL_HAND)
    return min(MAX_HANDS, BASE_HANDS + extra_hands)


def _market_orders(
    farm: dict[str, Any],
    private: dict[str, Any],
    market: dict[str, Any],
    day: int,
    hour: int,
    plan: FieldPlan,
) -> list[list[Any]]:
    """Sell products, buy seeds and animals, hire hands, and buy land."""
    orders: list[list[Any]] = []
    shed = _as_dict(private.get("shed"))
    seeds = _as_dict(private.get("seeds"))
    prices = _as_dict(market.get("prices"))
    money = float(farm.get("money", 0))

    force_sell = day >= FORCE_SELL_DAY
    shed_total = sum(shed.values())
    occupied_animals = sum(plan.occupied_by_animal.values())
    carried_animals = _carried_animal_counts(private)
    unplaced_animals = {
        animal: max(0, int(shed.get(animal, 0))) + carried_animals[animal]
        for animal in PLANTABLE_ANIMALS
    }
    known_animals = {
        animal: plan.occupied_by_animal.get(animal, 0) + unplaced_animals[animal]
        for animal in PLANTABLE_ANIMALS
    }
    total_animals = sum(known_animals.values())
    wheat_feed_reserve = total_animals * FEED_DAYS_BUFFER
    wheat_price = max(1, int(prices.get("WHEAT", 0)))
    wheat_stock = max(0, int(shed.get("WHEAT", 0)))
    # Retain enough cash to fully replace the physical feed buffer once,
    # even after buying seeds or hands.  This is intentionally separate from
    # the wheat in the shed: a harvest/replant gap should trigger an orderly
    # refill, not force a newly purchased herd to consume its last food.
    cash_floor = (
        ANIMAL_CASH_RESERVE + total_animals * EMERGENCY_FEED_DAYS * wheat_price
        if total_animals > 0
        else 0
    )
    # Once the wheat cap and an empty pasture make the next cow legal, do
    # not let seed or hiring orders consume the first fully funded increment.
    # This is a reservation, not a speculative purchase: the animal loop
    # below independently rechecks feed capacity, the slot, and season time.
    cow_purchase_floor = 0
    if (
        day >= FIRST_ANIMAL_BUY_DAY
        and plan.feed_supported_herd > total_animals
        and known_animals["COW"] < plan.animal_targets.get("COW", 0)
        and plan.empty_structures.get("PASTURE")
    ):
        cow_purchase_floor = (
            ANIMALS["COW"].cost
            + FEED_DAYS_BUFFER * wheat_price
            + ANIMAL_CASH_RESERVE
            + EMERGENCY_FEED_DAYS * wheat_price
        )
    spending_floor = max(cash_floor, cow_purchase_floor)

    # Feed is the only urgent market action.  It goes first so a crowded
    # revenue turn cannot silently drop the order that keeps the herd alive.
    wheat_shortfall = max(0, wheat_feed_reserve - wheat_stock)
    if wheat_shortfall > 0:
        affordable_wheat = min(wheat_shortfall, int(money // wheat_price))
        if affordable_wheat > 0:
            orders.append(["BUY_PRODUCT", "WHEAT", affordable_wheat])
            money -= affordable_wheat * wheat_price
            wheat_stock += affordable_wheat

    # Each product sells once its price clears a threshold tuned to how hard
    # it crashes under oversupply, unless the shed is about to overflow or
    # the season is ending and everything has to be liquidated regardless.
    # Do not liquidate feed until the final action day: the day-27 cash-out
    # window still has two end-of-day refreshes in which animals can escape.
    for product, (base_price, threshold_ratio) in SELL_SPECS.items():
        in_shed = int(shed.get(product, 0))
        if product == "WHEAT" and day < FINAL_ACTION_DAY:
            in_shed = max(0, in_shed - wheat_feed_reserve)
        if in_shed <= 0:
            continue
        price = int(prices.get(product, 0))
        is_cash_crop = product in CASH_CROP_PRODUCTS
        effective_threshold_ratio = (
            _sell_threshold_ratio(product, threshold_ratio, plan.demand)
            if is_cash_crop
            else threshold_ratio
        )
        threshold_met = price >= effective_threshold_ratio * base_price
        capacity_pressure = shed_total >= SHED_CAPACITY - plan.field_capacity
        # Only cash crops wait for a post-town-drain window: animal products
        # keep livestock-stack's original, already-conservative timing so
        # this integration doesn't touch what made that strategy robust.
        in_sale_window = (
            _post_town_drain_window(product, plan.demand, hour) if is_cash_crop else True
        )
        if not ((threshold_met and in_sale_window) or capacity_pressure or force_sell):
            continue
        min_batch = MIN_SELL_BATCH.get(product, 1)
        if not force_sell and not capacity_pressure and in_shed < min_batch:
            continue

        quantity = in_shed
        cap = MAX_SELL_PER_TURN.get(product)
        if cap is not None and not force_sell:
            quantity = min(quantity, cap)
        if quantity <= 0:
            continue
        if len(orders) >= MAX_MARKET_ORDERS:
            break
        orders.append(["SELL", product, quantity])
        shed_total -= quantity
        money += price * quantity

    # Seeds are bought before mature plants are cleared, eliminating an idle
    # turn between a harvest and planting the next cycle for that crop.
    owned_seeds = {crop: max(0, int(seeds.get(crop, 0))) for crop in PLANTABLE_CROPS}
    for crop in PLANTABLE_CROPS:
        if len(orders) >= MAX_MARKET_ORDERS:
            break
        if day > LAST_PLANT_DAY[crop]:
            continue
        projected_active = plan.crop_active[crop] - plan.crop_maturing[crop]
        deficit = max(0, plan.crop_targets.get(crop, 0) - projected_active - owned_seeds[crop])
        if deficit <= 0:
            continue
        affordable = min(
            deficit,
            int(max(0.0, money - spending_floor) // CROPS[crop].seed_cost),
        )
        if affordable <= 0:
            continue
        orders.append(["BUY_SEED", crop, affordable])
        money -= affordable * CROPS[crop].seed_cost

    # Staffing ramps from six hands to eleven with the committed herd. Hires
    # precede expansion purchases so a new feed obligation never arrives
    # without the workers needed to service it.
    if day <= LAST_HAND_HIRE_DAY:
        hires_today = int(farm.get("hires_today", 0))
        current_hands = len(farm.get("hands", []))
        desired_hands = _desired_hand_count(total_animals)
        while current_hands < desired_hands and len(orders) < MAX_MARKET_ORDERS:
            cost = _fib(hires_today)
            if money - cost < spending_floor:
                break
            orders.append(["HIRE"])
            money -= cost
            hires_today += 1
            current_hands += 1

    # A purchase requires all three safety gates: active wheat capacity,
    # an already-built and unreserved matching structure, and enough cash to
    # buy the animal plus the full five-day food reserve while retaining cash
    # for ordinary farm operations.  Bought animals therefore never wait in
    # a crowded shed for a future, hypothetical feed source.
    remaining_slots = {
        kind: max(
            0,
            len(plan.empty_structures.get(kind, []))
            - sum(
                unplaced_animals[animal]
                for animal in PLANTABLE_ANIMALS
                if ANIMALS[animal].structure == kind
            ),
        )
        for kind in STRUCTURE_KINDS
    }
    animals_bought_this_turn = 0
    planned_total = total_animals
    feed_ready = (
        wheat_stock >= wheat_feed_reserve
        and plan.feed_supported_herd >= total_animals
    )
    for animal in PLANTABLE_ANIMALS:
        if (
            not feed_ready
            or day < FIRST_ANIMAL_BUY_DAY
            or day > LAST_ANIMAL_BUY_DAY[animal]
            or animals_bought_this_turn >= MAX_NEW_ANIMALS_PER_TURN
        ):
            continue
        structure = ANIMALS[animal].structure
        deficit = max(0, plan.animal_targets.get(animal, 0) - known_animals[animal])
        herd_headroom = max(0, plan.feed_supported_herd - planned_total)
        quantity = min(
            deficit,
            remaining_slots[structure],
            herd_headroom,
            MAX_NEW_ANIMALS_PER_TURN - animals_bought_this_turn,
        )
        while quantity > 0:
            required_feed = (planned_total + quantity) * FEED_DAYS_BUFFER
            extra_wheat = max(0, required_feed - wheat_stock)
            total_cost = quantity * ANIMALS[animal].cost + extra_wheat * wheat_price
            next_cash_floor = (
                ANIMAL_CASH_RESERVE
                + (planned_total + quantity) * EMERGENCY_FEED_DAYS * wheat_price
            )
            required_orders = 1 + int(extra_wheat > 0)
            if (
                len(orders) + required_orders <= MAX_MARKET_ORDERS
                and money - total_cost >= next_cash_floor
            ):
                break
            quantity -= 1
        if quantity <= 0:
            continue
        required_feed = (planned_total + quantity) * FEED_DAYS_BUFFER
        extra_wheat = max(0, required_feed - wheat_stock)
        if extra_wheat > 0:
            orders.append(["BUY_PRODUCT", "WHEAT", extra_wheat])
            money -= extra_wheat * wheat_price
            wheat_stock += extra_wheat
        orders.append(["BUY_ANIMAL", animal, quantity])
        money -= quantity * ANIMALS[animal].cost
        known_animals[animal] += quantity
        planned_total += quantity
        remaining_slots[structure] -= quantity
        animals_bought_this_turn += quantity

    # Unlock NE once the wheat bootstrap and 60% utilization prove that the
    # initial field is operational.  Placing capital behind the first
    # feed-safe cow comes first; land only spends what remains after that
    # herd commitment and its emergency cash reserve.
    unlocked_quadrants = farm.get("unlocked_quadrants", ["NW"])
    n_extra_unlocked = max(0, len(unlocked_quadrants) - 1)
    if n_extra_unlocked < len(LAND_ORDER) and len(orders) < MAX_MARKET_ORDERS:
        next_quadrant = LAND_ORDER[n_extra_unlocked]
        land_cost = LAND_PRICES[next_quadrant]
        utilized = sum(plan.crop_active.values()) + sum(plan.structures_built.values())
        early_ne_ready = (
            next_quadrant == "NE"
            and day >= EARLY_NE_MIN_DAY
            and plan.wheat_active >= WHEAT_BOOTSTRAP_TILES
            and utilized >= math.ceil(plan.field_capacity * EARLY_NE_UTILIZATION_RATIO)
        )
        saturated = utilized >= plan.field_capacity - 1
        post_purchase_floor = (
            ANIMAL_CASH_RESERVE + planned_total * EMERGENCY_FEED_DAYS * wheat_price
            if planned_total > 0
            else 0
        )
        reserve = max(
            post_purchase_floor,
            EARLY_NE_CASH_RESERVE if next_quadrant == "NE" else LAND_CASH_RESERVE,
        )
        foundation_ready = total_animals > 0
        land_ready = (
            (early_ne_ready and foundation_ready)
            or (saturated and (next_quadrant != "NE" or foundation_ready))
        )
        if land_ready and money >= land_cost + reserve:
            orders.append(["BUY_LAND"])
            money -= land_cost

    return orders


def agent(obs: dict[str, Any]) -> dict[str, Any]:
    """Return one legal action for the farmer and every hired hand."""
    farms = obs.get("farms", [])
    player = int(obs.get("player", 0))
    if not isinstance(farms, list) or not 0 <= player < len(farms):
        return {"farmer": ["PASS"], "hands": [], "market": []}

    farm = _as_dict(farms[player])
    private = _as_dict(obs.get("private"))
    market = _as_dict(obs.get("market"))
    town = _as_dict(obs.get("town"))
    day = int(obs.get("day", 0))
    hour = int(obs.get("hour", 0))
    board_size = len(farm.get("tiles", []))
    farmer_position = tuple(farm.get("farmer", (0, 0)))
    hand_positions = [tuple(position) for position in farm.get("hands", [])]
    unit_positions = [farmer_position, *hand_positions]

    plan = _field_tasks(farm, private, day, town)
    fill_priority = _animal_fill_priority(plan)
    logistics = _logistics_actions(unit_positions, private, board_size, plan, fill_priority)

    remaining_positions = [pos for pos, act in zip(unit_positions, logistics) if act is None]
    remaining_actions = iter(_assign_actions(remaining_positions, plan.tasks))
    unit_actions = [act if act is not None else next(remaining_actions) for act in logistics]

    market_orders = _market_orders(farm, private, market, day, hour, plan)

    return {
        "farmer": unit_actions[0],
        "hands": unit_actions[1:],
        "market": market_orders,
    }
