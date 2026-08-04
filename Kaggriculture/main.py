"""Kaggriculture submission entrypoint.

This controller is a deliberately pure crop portfolio.  Every unlocked tile
is allocated across wheat, carrot, tomato, and melon; it never spends turns,
land, or money on animal structures, livestock, or feed.  The policy keeps
the operational parts that made the historical multi-crop baseline strong:
proportional planting, price-aware sales, patient one-time harvests, weed
clearing, and a fresh daily crew of farm hands.
"""

from dataclasses import dataclass
from typing import Any


MAX_HANDS = 6
MAX_MARKET_ORDERS = 10
SHED_CAPACITY = 100
FORCE_SELL_DAY = 27
LAST_HAND_HIRE_DAY = 28
FINAL_ACTION_DAY = 29
SAFETY_BUFFER_DAYS = 2

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
    sell_threshold_ratio: float
    allocation_ratio: float


# The historical all-crop baseline split every unlocked tile 30/25/30/15
# across staple, fast-cycle, ongoing, and premium crops respectively.
CROPS = {
    "WHEAT": CropSpec(10, 2, 4, 6, False, 25, 0.65, 0.30),
    "CARROT": CropSpec(20, 2, 3, 4, False, 35, 0.70, 0.25),
    "TOMATO": CropSpec(50, 8, 8, 4, True, 60, 0.65, 0.30),
    "MELON": CropSpec(80, 10, 12, 6, False, 250, 0.80, 0.15),
}
PLANTABLE_CROPS = ["WHEAT", "CARROT", "TOMATO", "MELON"]

# Do not start a crop unless it can mature, be harvested, and reach the
# market before the season's final action day.
LAST_PLANT_DAY = {
    crop: FINAL_ACTION_DAY
    - (spec.first_yield_day if spec.ongoing else spec.max_yield_day)
    - SAFETY_BUFFER_DAYS
    for crop, spec in CROPS.items()
}

# Premium melons are sold in smaller batches while the season is active so
# one order does not collapse their market price.  End-game liquidation is
# intentionally uncapped.
MAX_SELL_PER_TURN = {"MELON": 5}


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


def _unlocked_positions(farm: dict[str, Any]) -> list[tuple[int, int]]:
    """Return usable farm tiles in stable near-shed order."""
    tiles = farm.get("tiles", [])
    if not isinstance(tiles, list) or not tiles:
        return []

    board_size = len(tiles)
    shed_corner = (max(0, board_size // 2 - 1), max(0, board_size // 2 - 1))
    positions = [
        (x, y)
        for y, row in enumerate(tiles)
        if isinstance(row, list)
        for x, tile in enumerate(row)
        if tile != "LOCKED"
    ]
    return sorted(
        positions,
        key=lambda position: (
            _manhattan(position, shed_corner),
            position[1],
            position[0],
        ),
    )


@dataclass(frozen=True)
class FieldScan:
    tasks: list[Task]
    crop_active: dict[str, int]
    crop_maturing: dict[str, int]
    empty: list[tuple[int, int]]
    weeds: list[tuple[int, int]]


def _scan_field(farm: dict[str, Any], day: int, positions: list[tuple[int, int]]) -> FieldScan:
    """Collect crop care, harvest, weed, and occupancy information."""
    tasks: list[Task] = []
    crop_active = {crop: 0 for crop in PLANTABLE_CROPS}
    crop_maturing = {crop: 0 for crop in PLANTABLE_CROPS}
    empty: list[tuple[int, int]] = []
    weeds: list[tuple[int, int]] = []

    for position in positions:
        tile = _tile_at(farm, position)
        if tile is None:
            empty.append(position)
            continue
        if not isinstance(tile, dict):
            continue

        if tile.get("kind") == "WEED":
            weeds.append(position)
            continue
        if tile.get("kind") != "PLANT":
            continue

        crop = tile.get("crop")
        spec = CROPS.get(crop)
        if spec is None:
            continue

        crop_active[crop] += 1
        age = day - int(tile.get("planted_day", day))
        watered = bool(tile.get("watered_today", False))
        yield_units = int(tile.get("yield_units", 0))

        # A one-time plant entering its final bonus day is projected out of
        # the active field so the replacement seed arrives before harvest.
        if not spec.ongoing and age >= spec.max_yield_day:
            crop_maturing[crop] += 1

        # Watering takes precedence over harvest.  This preserves the current
        # day's one-time yield bonus and prevents crops from becoming weeds.
        if not watered and (spec.ongoing or age <= spec.max_yield_day):
            tasks.append(Task(100, position, ["WATER"]))

        mature = age >= spec.first_yield_day
        if spec.ongoing:
            can_harvest = mature and yield_units > 0
        else:
            # Harvest at the hard yield cap or only after the bonus window.
            # Harvesting merely at first yield discards later watered yield.
            can_harvest = (
                mature
                and yield_units > 0
                and (yield_units >= spec.max_yield or age > spec.max_yield_day)
            )
        if can_harvest:
            tasks.append(Task(90, position, ["HARVEST"]))

    return FieldScan(
        tasks=tasks,
        crop_active=crop_active,
        crop_maturing=crop_maturing,
        empty=empty,
        weeds=weeds,
    )


def _allocate_by_ratio(budget: int, keys: list[str], ratio: dict[str, float]) -> dict[str, int]:
    """Allocate an integer field budget with deterministic largest remainders."""
    raw = {key: budget * ratio[key] for key in keys}
    targets = {key: int(raw[key]) for key in keys}
    leftover = budget - sum(targets.values())
    order = sorted(keys, key=lambda key: raw[key] - targets[key], reverse=True)
    for key in order[: max(0, leftover)]:
        targets[key] += 1
    return targets


@dataclass(frozen=True)
class FieldPlan:
    tasks: list[Task]
    field_capacity: int
    crop_active: dict[str, int]
    crop_maturing: dict[str, int]
    crop_targets: dict[str, int]


def _field_tasks(farm: dict[str, Any], private: dict[str, Any], day: int) -> FieldPlan:
    """Create crop tasks and reserve seeds for the proportional field plan."""
    positions = _unlocked_positions(farm)
    field_capacity = len(positions)
    scan = _scan_field(farm, day, positions)
    crop_targets = _allocate_by_ratio(
        field_capacity,
        PLANTABLE_CROPS,
        {crop: CROPS[crop].allocation_ratio for crop in PLANTABLE_CROPS},
    )
    tasks = list(scan.tasks)

    if day < FINAL_ACTION_DAY:
        # Clearing weeds is more valuable than retaining a blocked tile, but
        # it yields to crop care and harvests already scheduled this turn.
        tasks.extend(Task(50, position, ["DIG"]) for position in scan.weeds)

        seeds = _as_dict(private.get("seeds"))
        owned_seeds = {
            crop: max(0, int(seeds.get(crop, 0))) for crop in PLANTABLE_CROPS
        }
        crop_needs = {
            crop: max(
                0,
                crop_targets[crop]
                - (scan.crop_active[crop] - scan.crop_maturing[crop]),
            )
            for crop in PLANTABLE_CROPS
        }

        for position in scan.empty:
            options = [
                (crop_needs[crop], ["PLANT", crop])
                for crop in PLANTABLE_CROPS
                if (
                    owned_seeds[crop] > 0
                    and crop_needs[crop] > 0
                    and day <= LAST_PLANT_DAY[crop]
                )
            ]
            if not options:
                continue

            _, action = max(options, key=lambda option: option[0])
            crop = action[1]
            tasks.append(Task(40, position, action))
            owned_seeds[crop] -= 1
            crop_needs[crop] -= 1
    else:
        # New work on the last day cannot return from field inventory to the
        # market before the season ends, so reserve it for liquidation only.
        tasks = []

    return FieldPlan(
        tasks=tasks,
        field_capacity=field_capacity,
        crop_active=scan.crop_active,
        crop_maturing=scan.crop_maturing,
        crop_targets=crop_targets,
    )


def _assign_actions(
    positions: list[tuple[int, int]],
    tasks: list[Task],
) -> list[list[str]]:
    """Give every unit a distinct highest-priority target for this turn."""
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
        actions.append(
            task.action
            if position == task.position
            else _move_toward(position, task.position)
        )

    return actions


def _fib(n: int) -> int:
    first, second = 1, 1
    for _ in range(n):
        first, second = second, first + second
    return first


def _market_orders(
    farm: dict[str, Any],
    private: dict[str, Any],
    market: dict[str, Any],
    day: int,
    hour: int,
    plan: FieldPlan,
) -> list[list[Any]]:
    """Sell crops, replenish seeds, schedule hands, and expand crop land."""
    del hour  # Hires retry every turn until the daily roster is full.
    orders: list[list[Any]] = []
    shed = _as_dict(private.get("shed"))
    seeds = _as_dict(private.get("seeds"))
    prices = _as_dict(market.get("prices"))
    money = float(farm.get("money", 0))
    force_sell = day >= FORCE_SELL_DAY
    shed_total = sum(
        max(0, int(count))
        for count in shed.values()
        if isinstance(count, (int, float))
    )

    # Sell only into acceptable prices unless storage is under pressure or the
    # end-game requires liquidation.  Orders are placed before spending so
    # revenue-producing actions cannot lose the per-turn order budget.
    for crop in PLANTABLE_CROPS:
        in_shed = max(0, int(shed.get(crop, 0)))
        if in_shed == 0:
            continue

        spec = CROPS[crop]
        price = int(prices.get(crop, 0))
        threshold_met = price >= spec.sell_threshold_ratio * spec.base_price
        capacity_pressure = shed_total >= SHED_CAPACITY - plan.field_capacity
        if not (threshold_met or capacity_pressure or force_sell):
            continue

        quantity = in_shed
        sell_cap = MAX_SELL_PER_TURN.get(crop)
        if sell_cap is not None and not force_sell:
            quantity = min(quantity, sell_cap)
        if quantity <= 0:
            continue

        orders.append(["SELL", crop, quantity])
        shed_total -= quantity

    # Buy seeds before mature crops are removed so a replacement can be
    # planted at the first available action after harvest.
    owned_seeds = {
        crop: max(0, int(seeds.get(crop, 0))) for crop in PLANTABLE_CROPS
    }
    for crop in PLANTABLE_CROPS:
        if day > LAST_PLANT_DAY[crop]:
            continue

        projected_active = plan.crop_active[crop] - plan.crop_maturing[crop]
        deficit = max(
            0,
            plan.crop_targets[crop] - projected_active - owned_seeds[crop],
        )
        if deficit == 0:
            continue

        affordable = min(deficit, int(money // CROPS[crop].seed_cost))
        if affordable == 0:
            continue
        orders.append(["BUY_SEED", crop, affordable])
        money -= affordable * CROPS[crop].seed_cost

    # Farm hands reset each day.  Retrying after any crowded market turn
    # gives later turns a chance to fill the daily crew without special
    # hour-zero logic.
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

    # Expanding is intentionally conservative: only buy after the currently
    # unlocked crop field is effectively full and retain a working reserve.
    unlocked_quadrants = farm.get("unlocked_quadrants", ["NW"])
    extra_quadrants = max(0, len(unlocked_quadrants) - 1)
    if extra_quadrants < len(LAND_ORDER) and len(orders) < MAX_MARKET_ORDERS:
        next_quadrant = LAND_ORDER[extra_quadrants]
        land_cost = LAND_PRICES[next_quadrant]
        crop_tiles = sum(plan.crop_active.values())
        field_is_full = crop_tiles >= plan.field_capacity - 1
        if field_is_full and money >= land_cost + LAND_CASH_RESERVE:
            orders.append(["BUY_LAND"])

    return orders[:MAX_MARKET_ORDERS]


def agent(obs: dict[str, Any]) -> dict[str, Any]:
    """Return one legal action for the farmer and each currently hired hand."""
    farms = obs.get("farms", [])
    player = int(obs.get("player", 0))
    if not isinstance(farms, list) or not 0 <= player < len(farms):
        return {"farmer": ["PASS"], "hands": [], "market": []}

    farm = _as_dict(farms[player])
    private = _as_dict(obs.get("private"))
    market = _as_dict(obs.get("market"))
    day = int(obs.get("day", 0))
    hour = int(obs.get("hour", 0))
    farmer_position = tuple(farm.get("farmer", (0, 0)))
    hand_positions = [tuple(position) for position in farm.get("hands", [])]
    unit_positions = [farmer_position, *hand_positions]

    plan = _field_tasks(farm, private, day)
    unit_actions = _assign_actions(unit_positions, plan.tasks)
    market_orders = _market_orders(farm, private, market, day, hour, plan)

    return {
        "farmer": unit_actions[0],
        "hands": unit_actions[1:],
        "market": market_orders,
    }
