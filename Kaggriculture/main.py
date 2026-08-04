"""Kaggriculture submission entrypoint: a staple-crop velocity strategy.

The strategy only plants WHEAT and CARROT.  Wheat is the primary cash
engine because its market curve tolerates gluts; carrot is a smaller,
batch-sold complement whose output is throttled before its steep glut curve
destroys value.  The planting schedule is deliberately spread across four
daily cohorts so expanded production never produces more than the shed can hold
in a single end-of-day inventory drop.

NE is the only land purchase.  It is bought only after live staple crops
occupy at least 80% of the current field and the bank is at least $2,300:
the $1,000 purchase then leaves a $1,300 labor-and-seed reserve.
"""

from dataclasses import dataclass
from typing import Any


# The first five hires cost just $12/day total.  That is enough capacity to
# water the capped staple field without paying the Fibonacci premium of a
# sixth and later hand every day.
MAX_HANDS = 5
MAX_MARKET_ORDERS = 10
SHED_CAPACITY = 100
SHED_PRESSURE_START = 72
FINAL_ACTION_DAY = 29
LAST_HAND_HIRE_DAY = 28
COHORT_DAYS = 4
MAX_ACTIVE_CROP_TILES = 32

# The first land purchase costs $1,000.  Do not expand until the field is
# materially occupied and the remaining cash is enough to re-seed and staff
# the new space without depending on an unprocessed sale this turn.
NE_LAND_CASH_TRIGGER = 2300
NE_LAND_SATURATION_RATIO = 0.80


@dataclass(frozen=True)
class CropSpec:
    seed_cost: int
    first_yield_day: int
    max_yield_day: int
    max_yield: int
    base_price: int
    allocation_ratio: float


CROPS = {
    "WHEAT": CropSpec(
        seed_cost=10,
        first_yield_day=2,
        max_yield_day=4,
        max_yield=6,
        base_price=25,
        allocation_ratio=0.70,
    ),
    "CARROT": CropSpec(
        seed_cost=20,
        first_yield_day=2,
        max_yield_day=3,
        max_yield=4,
        base_price=35,
        allocation_ratio=0.30,
    ),
}
PLANTABLE_CROPS = tuple(CROPS)

# Planting on these days still leaves time to harvest at age > max_yield_day
# and sell the resulting end-of-day inventory before the season ends.
LAST_PLANT_DAY = {"WHEAT": 23, "CARROT": 24}


@dataclass(frozen=True)
class SellPolicy:
    min_price: int
    batch_size: int
    min_inventory: int
    pressure_batch: int


# Wheat sells in small, frequent batches whenever it remains worth at least
# $15.  Carrot waits for $30 and releases only eight units at once; its
# above-equilibrium price curve is much steeper, so this preserves town-led
# recovery between sales.  Shed pressure and endgame liquidation override
# those brakes to protect already-harvested output from being discarded.
SELL_POLICIES = {
    "WHEAT": SellPolicy(min_price=15, batch_size=24, min_inventory=1, pressure_batch=48),
    "CARROT": SellPolicy(min_price=30, batch_size=8, min_inventory=8, pressure_batch=16),
}
FORCE_SELL_DAY = 27


@dataclass(frozen=True)
class Task:
    priority: int
    position: tuple[int, int]
    action: list[str]


@dataclass(frozen=True)
class FieldScan:
    tasks: list[Task]
    crop_active: dict[str, int]
    crop_maturing: dict[str, int]
    crop_planted_today: dict[str, int]
    empty: list[tuple[int, int]]
    weeds: list[tuple[int, int]]


@dataclass(frozen=True)
class FieldPlan:
    tasks: list[Task]
    field_capacity: int
    crop_active: dict[str, int]
    crop_maturing: dict[str, int]
    crop_planted_today: dict[str, int]
    crop_targets: dict[str, int]


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _tile_at(farm: dict[str, Any], position: tuple[int, int]) -> Any:
    x, y = position
    return farm["tiles"][y][x]


def _manhattan(a: tuple[int, int], b: tuple[int, int]) -> int:
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def _move_toward(origin: tuple[int, int], destination: tuple[int, int]) -> list[str]:
    """Take one deterministic Manhattan step toward a target tile."""
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
    """Return usable tiles in a stable near-shed order."""
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
        key=lambda position: (
            _manhattan(position, shed_corner),
            position[1],
            position[0],
        ),
    )


def _scan_field(
    farm: dict[str, Any], day: int, positions: list[tuple[int, int]]
) -> FieldScan:
    """Scan staple plants for daily care, harvest, and planting work."""
    tasks: list[Task] = []
    crop_active = {crop: 0 for crop in PLANTABLE_CROPS}
    crop_maturing = {crop: 0 for crop in PLANTABLE_CROPS}
    crop_planted_today = {crop: 0 for crop in PLANTABLE_CROPS}
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
        planted_day = int(tile.get("planted_day", day))
        if planted_day == day:
            crop_planted_today[crop] += 1

        age = day - planted_day
        yield_units = int(tile.get("yield_units", 0))
        watered = bool(tile.get("watered_today", False))

        # Watering through max_yield_day gives every bonus-window increment.
        # At the first day after that window, harvest instead of spending an
        # action on watering a plant that is about to be removed.
        if not watered and age <= spec.max_yield_day:
            tasks.append(Task(100, position, ["WATER"]))

        mature = age >= spec.first_yield_day
        bonus_window_closed = age > spec.max_yield_day
        cap_reached = yield_units >= spec.max_yield
        can_harvest = yield_units > 0 and mature and (bonus_window_closed or cap_reached)
        if can_harvest:
            crop_maturing[crop] += 1
            # Once the bonus window has closed, one-time crops begin fast
            # turn-based decay.  Harvest before routine watering elsewhere;
            # the early hired workforce still has ample same-day capacity to
            # water every remaining staple afterward.
            tasks.append(Task(120, position, ["HARVEST"]))

    return FieldScan(
        tasks=tasks,
        crop_active=crop_active,
        crop_maturing=crop_maturing,
        crop_planted_today=crop_planted_today,
        empty=empty,
        weeds=weeds,
    )


def _crop_targets(field_capacity: int) -> dict[str, int]:
    """Allocate the staffable staple capacity 70% wheat and 30% carrot."""
    active_capacity = min(field_capacity, MAX_ACTIVE_CROP_TILES)
    wheat_target = round(active_capacity * CROPS["WHEAT"].allocation_ratio)
    return {"WHEAT": wheat_target, "CARROT": active_capacity - wheat_target}


def _daily_cohort_quota(target: int, day: int) -> int:
    """Split a target across four days to keep harvest drops shed-safe."""
    base, remainder = divmod(target, COHORT_DAYS)
    return base + int(day % COHORT_DAYS < remainder)


def _field_tasks(farm: dict[str, Any], private: dict[str, Any], day: int) -> FieldPlan:
    """Create only crop care/harvest/plant/dig work for this turn."""
    positions = _unlocked_positions(farm)
    scan = _scan_field(farm, day, positions)
    field_capacity = len(positions)
    crop_targets = _crop_targets(field_capacity)
    tasks = list(scan.tasks)

    if day < FINAL_ACTION_DAY:
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
        daily_remaining = {
            crop: max(
                0,
                _daily_cohort_quota(crop_targets[crop], day)
                - scan.crop_planted_today[crop],
            )
            for crop in PLANTABLE_CROPS
        }

        for position in scan.empty:
            choices = [
                crop
                for crop in PLANTABLE_CROPS
                if (
                    owned_seeds[crop] > 0
                    and crop_needs[crop] > 0
                    and daily_remaining[crop] > 0
                    and day <= LAST_PLANT_DAY[crop]
                )
            ]
            if not choices:
                continue
            crop = max(
                choices,
                key=lambda candidate: (
                    crop_needs[candidate],
                    daily_remaining[candidate],
                    -PLANTABLE_CROPS.index(candidate),
                ),
            )
            tasks.append(Task(40, position, ["PLANT", crop]))
            owned_seeds[crop] -= 1
            crop_needs[crop] -= 1
            daily_remaining[crop] -= 1
    else:
        # New field work on day 29 cannot turn into money before the season
        # ends, so reserve every market slot for liquidation.
        tasks = []

    return FieldPlan(
        tasks=tasks,
        field_capacity=field_capacity,
        crop_active=scan.crop_active,
        crop_maturing=scan.crop_maturing,
        crop_planted_today=scan.crop_planted_today,
        crop_targets=crop_targets,
    )


def _assign_actions(
    positions: list[tuple[int, int]], tasks: list[Task]
) -> list[list[str]]:
    """Give every unit a distinct urgent target, then move or act."""
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
            task.action if position == task.position else _move_toward(position, task.position)
        )
    return actions


def _fib(n: int) -> int:
    first, second = 1, 1
    for _ in range(n):
        first, second = second, first + second
    return first


def _append_sell_orders(
    orders: list[list[Any]],
    shed: dict[str, Any],
    prices: dict[str, Any],
    day: int,
    shed_total: int,
) -> int:
    """Queue product-specific sale batches and return remaining shed stock."""
    force_sell = day >= FORCE_SELL_DAY
    for product in PLANTABLE_CROPS:
        in_shed = max(0, int(shed.get(product, 0)))
        if in_shed <= 0 or len(orders) >= MAX_MARKET_ORDERS:
            continue

        policy = SELL_POLICIES[product]
        price = int(prices.get(product, 0))
        price_ready = price >= policy.min_price and in_shed >= policy.min_inventory
        capacity_pressure = shed_total >= SHED_PRESSURE_START
        if force_sell:
            quantity = in_shed
        elif price_ready:
            quantity = min(in_shed, policy.batch_size)
        elif capacity_pressure:
            quantity = min(in_shed, policy.pressure_batch)
        else:
            continue

        orders.append(["SELL", product, quantity])
        shed_total -= quantity
    return shed_total


def _market_orders(
    farm: dict[str, Any],
    private: dict[str, Any],
    market: dict[str, Any],
    day: int,
    hour: int,
    plan: FieldPlan,
) -> list[list[Any]]:
    """Sell staples, buy replacement seeds, hire early labor, and buy NE."""
    del hour  # The policy is intentionally valid on every turn of the day.
    orders: list[list[Any]] = []
    shed = _as_dict(private.get("shed"))
    seeds = _as_dict(private.get("seeds"))
    prices = _as_dict(market.get("prices"))
    money = float(farm.get("money", 0))
    shed_total = sum(max(0, int(count)) for count in shed.values())

    _append_sell_orders(orders, shed, prices, day, shed_total)

    # Buy ahead of a mature harvest so the next cohort can plant on the first
    # turn after the tile clears.  Seeds are unbounded and do not consume shed
    # capacity, so holding only the planned deficit is safe.
    owned_seeds = {
        crop: max(0, int(seeds.get(crop, 0))) for crop in PLANTABLE_CROPS
    }
    for crop in PLANTABLE_CROPS:
        if day > LAST_PLANT_DAY[crop] or len(orders) >= MAX_MARKET_ORDERS:
            continue
        projected_active = plan.crop_active[crop] - plan.crop_maturing[crop]
        deficit = max(0, plan.crop_targets[crop] - projected_active - owned_seeds[crop])
        affordable = min(deficit, int(money // CROPS[crop].seed_cost))
        if affordable <= 0:
            continue
        orders.append(["BUY_SEED", crop, affordable])
        money -= affordable * CROPS[crop].seed_cost

    # Cheap early labor is what makes daily watering, staggered harvesting,
    # and immediate replants reliable.  Retry every hour in case prior sales
    # or seed orders consumed the ten-order market budget at hour zero.
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

    # This crop-only candidate never purchases SW/SE.  The NE trigger is
    # intentionally expressed against current bank cash, not expected sale
    # income, so it cannot strand the enlarged field after a weak sale turn.
    unlocked = list(farm.get("unlocked_quadrants", ["NW"]))
    live_staples = sum(plan.crop_active.values())
    saturation_required = round(plan.field_capacity * NE_LAND_SATURATION_RATIO)
    only_nw_unlocked = len(unlocked) == 1 and "NW" in unlocked
    if (
        only_nw_unlocked
        and live_staples >= saturation_required
        and money >= NE_LAND_CASH_TRIGGER
        and len(orders) < MAX_MARKET_ORDERS
    ):
        orders.append(["BUY_LAND"])

    return orders[:MAX_MARKET_ORDERS]


def agent(obs: dict[str, Any]) -> dict[str, Any]:
    """Return one legal crop-only action for the farmer and each farm hand."""
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
