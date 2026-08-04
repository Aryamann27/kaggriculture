"""Kaggriculture submission entrypoint.

This strategy runs a hybrid crop-plus-livestock economy. It treats the
public list of unlocked town shops as observed demand: wheat remains the
low-risk fallback, while crop and livestock targets shift only toward
products demanded by shops already on the board. Premium output is capped
and sold in demand-sized batches after town consumption, reducing exposure
to the shared market's steep glut curves. Animals are bought only out of a
cash *surplus* above a healthy reserve and wheat is kept for feed, so demand
response never creates a fragile boom-bust ramp.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


MAX_HANDS = 6
SHED_CAPACITY = 100
FORCE_SELL_DAY = 27
LAST_HAND_HIRE_DAY = 28
FINAL_ACTION_DAY = 29
MAX_MARKET_ORDERS = 10
SAFETY_BUFFER_DAYS = 2

# Crops get the majority land share as the primary, low-risk cash engine;
# animals get the remainder plus whatever extra wheat their feed demands.
CROP_LAND_SHARE = 0.70

MIN_WHEAT_TILES = 4
# Buffer over the ~0.8 units/tile/day unfertilized wheat rate so feed supply
# stays ahead of a growing animal population.
WHEAT_TILES_PER_ANIMAL = 1.5
# Animals escape after 2 unfed days, but wheat takes 2-4 days to first
# yield. Require a small wheat buffer in the shed before placing a *new*
# animal, so it never starts its starvation clock with zero feed available.
MIN_WHEAT_BUFFER_FOR_PLACEMENT = 3
# How many days of feed the bootstrap wheat purchase (see _market_orders)
# tries to keep in reserve for the *entire* current+pending animal
# population, not just enough for one placement.
FEED_DAYS_BUFFER = 3
# Animals are bought only from cash *surplus* above this floor, and only a
# few per turn -- both exist so buying toward a large deficit can't drain
# the bank in one shot and starve the population it just bought. Crop
# revenue (not the starting bank) is meant to fund this reserve growing
# over time.
ANIMAL_CASH_RESERVE = 1200
MAX_NEW_ANIMALS_PER_TURN = 2

LAND_ORDER = ["NE", "SW", "SE"]
LAND_PRICES = {"NE": 1000, "SW": 2000, "SE": 4000}
LAND_CASH_RESERVE = 500


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
    "WHEAT":      CropSpec(10, 2, 4, 6, False, base_price=25, sell_threshold_ratio=0.65, allocation_ratio=0.30),
    "CARROT":     CropSpec(20, 2, 3, 4, False, base_price=35, sell_threshold_ratio=0.70, allocation_ratio=0.25),
    "TOMATO":     CropSpec(50, 8, 8, 4, True, base_price=60, sell_threshold_ratio=0.65, allocation_ratio=0.30),
    "STRAWBERRY": CropSpec(100, 10, 10, 4, True, base_price=120, sell_threshold_ratio=0.75, allocation_ratio=0.0),
    "MELON":      CropSpec(80, 10, 12, 6, False, base_price=250, sell_threshold_ratio=0.80, allocation_ratio=0.15),
}

# Strawberry is only allocated when an already-unlocked shop consumes it.
# Its small target cap below prevents a slow, premium crop from dominating
# the field if a single shop happens to unlock early.
PLANTABLE_CROPS = ["WHEAT", "CARROT", "TOMATO", "STRAWBERRY", "MELON"]

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
    "COW":   AnimalSpec(400, "PASTURE", 8, 2, 6, base_price=160, sell_threshold_ratio=0.80, allocation_ratio=0.50),
    "SHEEP": AnimalSpec(500, "PASTURE", 6, 3, 6, base_price=200, sell_threshold_ratio=0.85, allocation_ratio=0.30),
    "GOOSE": AnimalSpec(300, "COOP", 4, 1, 4, base_price=50, sell_threshold_ratio=0.65, allocation_ratio=0.20),
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
# curve in one shot. Premium products are therefore capped at the observed
# quantity the town just consumed, while staple caps remain deliberately
# wider so wheat stays a practical fallback crop.
PREMIUM_PRODUCTS = {"STRAWBERRY", "MELON", "MILK", "WOOL"}
STAPLE_SELL_CAP = {"WHEAT": 6, "CARROT": 4, "TOMATO": 4, "EGG": 3}

# The observation does not expose configuration, so these match the public
# default rules. A score measures demand per town-center interval: each shop
# runs three times in that interval and single-product shops consume twice.
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


def _wheat_feed_demand(animal_count: int) -> int:
    """Tiles of wheat wanted purely to cover animal feed (on top of
    whatever wheat's normal crop-ratio share already provides)."""
    return math.ceil(animal_count * WHEAT_TILES_PER_ANIMAL) if animal_count > 0 else 0


@dataclass(frozen=True)
class TownDemand:
    """Observed product demand, without predicting future shop unlocks."""

    score: dict[str, int]
    shop_units: dict[str, int]
    center_units: int


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

    return TownDemand(score=score, shop_units=shop_units, center_units=center_units)


def _demand_adjusted_crop_targets(budget: int, demand: TownDemand) -> dict[str, int]:
    """Allocate crop tiles toward known town demand, retaining wheat safety."""
    weights = {
        # Wheat is always useful for feed and is comparatively glut-resistant.
        "WHEAT": 3.0 + 0.25 * demand.score["WHEAT"],
        "CARROT": 2.2 + 0.35 * demand.score["CARROT"],
        "TOMATO": 2.5 + 0.35 * demand.score["TOMATO"],
        # Do not speculate on strawberry before a real shop asks for it.
        "STRAWBERRY": (
            0.25 + 0.35 * demand.score["STRAWBERRY"]
            if demand.shop_units["STRAWBERRY"] > 0
            else 0.0
        ),
        # No town shop consumes melons, so keep only a small diversification
        # allocation and rely on strict sale chunking to avoid a premium glut.
        "MELON": 0.25,
    }
    total_weight = sum(weights.values())
    ratios = {crop: weight / total_weight for crop, weight in weights.items()}
    targets = _allocate_by_ratio(budget, PLANTABLE_CROPS, ratios)
    for crop, share in {"STRAWBERRY": 0.15, "MELON": 0.10}.items():
        cap = max(1, int(budget * share))
        overflow = max(0, targets[crop] - cap)
        if overflow:
            targets[crop] -= overflow
            targets["WHEAT"] += overflow
    return targets


def _demand_adjusted_animal_targets(budget: int, demand: TownDemand) -> dict[str, int]:
    """Favor animals whose product is consumed by known shops, not guesses."""
    weights = {
        "COW": 1.0 + 0.45 * demand.shop_units["MILK"],
        "SHEEP": 0.4 + 0.50 * demand.shop_units["WOOL"],
        "GOOSE": 0.8 + 0.60 * demand.shop_units["EGG"],
    }
    total_weight = sum(weights.values())
    ratios = {animal: weight / total_weight for animal, weight in weights.items()}
    targets = _allocate_by_ratio(budget, PLANTABLE_ANIMALS, ratios)

    # At most 60% of the observed town demand is allocated to our herd. The
    # remainder is headroom for the opponent and for market volatility. This
    # is especially important for milk and wool, which can collapse to $1.
    daily_output = {"COW": 0.5, "SHEEP": 1 / 3, "GOOSE": 1.0}
    hard_caps = {"COW": 6, "SHEEP": 4, "GOOSE": 6}
    for animal, product in ANIMAL_PRODUCT.items():
        daily_town_demand = 2 * demand.score[product]
        demand_cap = max(1, math.ceil(0.60 * daily_town_demand / daily_output[animal]))
        targets[animal] = min(targets[animal], demand_cap, hard_caps[animal])
    return targets


@dataclass(frozen=True)
class FieldPlan:
    tasks: list[Task]
    field_capacity: int
    crop_active: dict[str, int]
    crop_maturing: dict[str, int]
    crop_targets: dict[str, int]
    animal_targets: dict[str, int]
    demand: TownDemand
    occupied_by_animal: dict[str, int]
    structures_built: dict[str, int]
    empty_structures: dict[str, list[tuple[int, int]]]
    unfed_animals: list[tuple[int, int]]
    fertilizable_wheat: list[tuple[int, int]]


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

    shed = _as_dict(private.get("shed"))
    owned_animals_total = sum(int(shed.get(a, 0)) for a in PLANTABLE_ANIMALS)
    animal_count = sum(scan.occupied_by_animal.values()) + owned_animals_total

    crop_budget = round(field_capacity * CROP_LAND_SHARE)
    livestock_budget = max(0, field_capacity - crop_budget)
    crop_targets = _demand_adjusted_crop_targets(crop_budget, demand)
    # Wheat also has to cover animal feed; if that demand exceeds its normal
    # crop-ratio share, let it claim more of the shared empty-tile
    # competition below rather than capping it -- a smaller tomato patch is
    # a much better trade than a starving herd.
    crop_targets["WHEAT"] = max(
        MIN_WHEAT_TILES, crop_targets["WHEAT"], _wheat_feed_demand(animal_count)
    )

    animal_targets = _demand_adjusted_animal_targets(livestock_budget, demand)
    structure_targets = {kind: 0 for kind in STRUCTURE_KINDS}
    for animal in PLANTABLE_ANIMALS:
        structure_targets[ANIMALS[animal].structure] += animal_targets[animal]

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
        demand=demand,
        occupied_by_animal=scan.occupied_by_animal,
        structures_built=scan.structures_built,
        empty_structures=empty_structures,
        unfed_animals=unfed_animals,
        fertilizable_wheat=fertilizable_wheat,
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

    actions: list[list[str] | None] = []
    for idx, position in enumerate(unit_positions):
        inv = _as_dict(inventories[idx]) if idx < len(inventories) else {}

        carried_animal = next((a for a in PLANTABLE_ANIMALS if inv.get(a, 0) > 0), None)
        if carried_animal is not None:
            targets = remaining_structures.get(ANIMALS[carried_animal].structure, [])
            if not targets:
                actions.append(None)
                continue
            target = min(targets, key=lambda p: _manhattan(position, p))
            if position == target:
                actions.append(["PLACE", carried_animal])
                targets.remove(target)
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
                # Don't start a new animal's starvation clock with no feed
                # on hand -- wheat takes 2-4 days to first yield, but two
                # unfed days is fatal.
                and remaining_shed_wheat >= MIN_WHEAT_BUFFER_FOR_PLACEMENT
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


def _post_town_drain_window(product: str, demand: TownDemand, hour: int) -> bool:
    """Sell just after the town has drained this product's inventory."""
    interval = TOWN_SHOP_INTERVAL if demand.shop_units.get(product, 0) else TOWN_CENTER_INTERVAL
    return hour % interval == 1


def _sale_quantity_cap(product: str, demand: TownDemand, hour: int) -> int:
    """Limit premium replenishment to the amount town just consumed."""
    drained = 0
    if demand.shop_units.get(product, 0) and hour % TOWN_SHOP_INTERVAL == 1:
        drained += demand.shop_units[product]
    if hour % TOWN_CENTER_INTERVAL == 1:
        drained += demand.center_units

    if product in PREMIUM_PRODUCTS:
        return max(1, drained)
    return max(STAPLE_SELL_CAP.get(product, 1), drained)


def _sell_threshold_ratio(product: str, threshold_ratio: float, demand: TownDemand) -> float:
    """Known demand permits a modestly earlier sale without betting on demand."""
    discount = min(0.15, 0.025 * demand.shop_units.get(product, 0))
    return max(0.50, threshold_ratio - discount)


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

    # Wheat doubles as animal feed, so a chunk of it is never for sale: the
    # sell loop below must reserve this buffer before offering any surplus,
    # otherwise a high wheat price sells off the very stock animals need
    # (our own crop takes 2-4+ days to yield, longer than the 2-unfed-day
    # grace period animals get before they escape).
    occupied_animals = sum(plan.occupied_by_animal.values())
    owned_unplaced_animals = sum(int(shed.get(a, 0)) for a in PLANTABLE_ANIMALS)
    total_animals = occupied_animals + owned_unplaced_animals
    wheat_feed_reserve = (
        max(MIN_WHEAT_BUFFER_FOR_PLACEMENT, total_animals * FEED_DAYS_BUFFER)
        if total_animals > 0
        else 0
    )

    # Each product sells once its price clears a threshold tuned to how hard
    # it crashes under oversupply. Hold normal sales until the turn after the
    # town consumed that product: market orders run before town consumption,
    # so selling on the consumption tick itself would refill inventory before
    # the town has a chance to drain it. Shed pressure and end-game
    # liquidation override the timing guard.
    for product, (base_price, threshold_ratio) in SELL_SPECS.items():
        in_shed = int(shed.get(product, 0))
        if product == "WHEAT" and not force_sell:
            in_shed = max(0, in_shed - wheat_feed_reserve)
        if in_shed <= 0:
            continue
        price = int(prices.get(product, 0))
        threshold_met = price >= _sell_threshold_ratio(product, threshold_ratio, plan.demand) * base_price
        capacity_pressure = shed_total >= SHED_CAPACITY - plan.field_capacity
        in_sale_window = _post_town_drain_window(product, plan.demand, hour)
        if not ((threshold_met and in_sale_window) or capacity_pressure or force_sell):
            continue

        quantity = in_shed
        if not (capacity_pressure or force_sell):
            quantity = min(quantity, _sale_quantity_cap(product, plan.demand, hour))
        if quantity <= 0:
            continue
        orders.append(["SELL", product, quantity])
        shed_total -= quantity

    # Bootstrap: top up the wheat buffer directly from the market if it's
    # too thin to safely feed/place animals. Without this, newly bought
    # animals would sit idle in the shed (or worse, starve once placed)
    # waiting on a self-grown feed source that hasn't caught up yet. This
    # should rarely fire in the hybrid model since wheat is also a
    # ratio-funded crop from day one, not solely demand-driven.
    if total_animals > 0:
        wheat_in_shed = int(shed.get("WHEAT", 0))
        wheat_shortfall = max(0, wheat_feed_reserve - wheat_in_shed)
        if wheat_shortfall > 0:
            wheat_price = max(1, int(prices.get("WHEAT", 0)))
            affordable = min(wheat_shortfall, int(money // wheat_price))
            if affordable > 0:
                orders.append(["BUY_PRODUCT", "WHEAT", affordable])
                money -= affordable * wheat_price

    # Seeds are bought before mature plants are cleared, eliminating an idle
    # turn between a harvest and planting the next cycle for that crop.
    owned_seeds = {crop: max(0, int(seeds.get(crop, 0))) for crop in PLANTABLE_CROPS}
    for crop in PLANTABLE_CROPS:
        if day > LAST_PLANT_DAY[crop]:
            continue
        projected_active = plan.crop_active[crop] - plan.crop_maturing[crop]
        deficit = max(0, plan.crop_targets.get(crop, 0) - projected_active - owned_seeds[crop])
        if deficit <= 0:
            continue
        affordable = min(deficit, int(money // CROPS[crop].seed_cost))
        if affordable <= 0:
            continue
        orders.append(["BUY_SEED", crop, affordable])
        money -= affordable * CROPS[crop].seed_cost

    # Animals are bought toward their target population minus however many
    # are already alive or already sitting in the shed awaiting placement,
    # but only from cash *surplus* above ANIMAL_CASH_RESERVE -- crop revenue,
    # not the starting bank, is meant to fund this. Each purchase's
    # *effective* cost also includes its own feed-bootstrap reserve
    # (FEED_DAYS_BUFFER worth of wheat at the current price), and a small
    # per-turn cap adds a second, independent brake -- both exist so a large
    # deficit can't drain the bank in one shot and starve the population it
    # just bought.
    animals_bought_this_turn = 0
    wheat_price_estimate = max(1, int(prices.get("WHEAT", 0)))
    for animal in PLANTABLE_ANIMALS:
        if day > LAST_ANIMAL_BUY_DAY[animal] or animals_bought_this_turn >= MAX_NEW_ANIMALS_PER_TURN:
            continue
        owned_unplaced = int(shed.get(animal, 0))
        deficit = max(
            0,
            plan.animal_targets.get(animal, 0) - plan.occupied_by_animal.get(animal, 0) - owned_unplaced,
        )
        if deficit <= 0:
            continue
        remaining_quota = MAX_NEW_ANIMALS_PER_TURN - animals_bought_this_turn
        effective_cost = ANIMALS[animal].cost + FEED_DAYS_BUFFER * wheat_price_estimate
        available_cash = max(0.0, money - ANIMAL_CASH_RESERVE)
        affordable = min(deficit, remaining_quota, int(available_cash // effective_cost))
        if affordable <= 0:
            continue
        orders.append(["BUY_ANIMAL", animal, affordable])
        money -= affordable * ANIMALS[animal].cost
        animals_bought_this_turn += affordable

    # Hands disappear each end of day and hires only fill whatever order
    # budget remains after sells/buys above -- with up to 4 crops and 3
    # animal products all potentially selling at once, hour 0 alone can be
    # crowded out of its entire budget some days. Retrying every hour (not
    # just hour 0) makes this self-healing: any later hour with spare budget
    # still picks up the remaining hires, rather than going hand-less for
    # the whole day.
    if day <= LAST_HAND_HIRE_DAY:
        hires_today = int(farm.get("hires_today", 0))
        current_hands = len(farm.get("hands", []))
        while current_hands < MAX_HANDS and len(orders) < MAX_MARKET_ORDERS:
            cost = _fib(hires_today)
            if money < cost:
                break
            orders.append(["HIRE"])
            money -= cost
            hires_today += 1
            current_hands += 1

    # Land is worth buying once the current quadrant is essentially full of
    # intentional use, extending both the cash-crop engine and the
    # livestock population's runway.
    unlocked_quadrants = farm.get("unlocked_quadrants", ["NW"])
    n_extra_unlocked = max(0, len(unlocked_quadrants) - 1)
    if n_extra_unlocked < len(LAND_ORDER) and len(orders) < MAX_MARKET_ORDERS:
        next_quadrant = LAND_ORDER[n_extra_unlocked]
        land_cost = LAND_PRICES[next_quadrant]
        utilized = sum(plan.crop_active.values()) + sum(plan.structures_built.values())
        saturated = utilized >= plan.field_capacity - 1
        if saturated and money >= land_cost + LAND_CASH_RESERVE:
            orders.append(["BUY_LAND"])
            money -= land_cost

    return orders[:MAX_MARKET_ORDERS]


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
