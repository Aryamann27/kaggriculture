# Development Notes

## Local setup

The project uses the existing uv-managed `.venv`.

```bash
uv sync
uv run python -m unittest discover -s tests -v
uv run python run_local.py --opponent starter --episodes 10 --seed 100
```

`main.py` is the Kaggle submission entrypoint. It has no project-local imports,
so it can be uploaded as a single-file submission.

## Strategy

The agent runs a **hybrid crop-plus-livestock economy**. Crops are the
primary land use and the funded cash engine; animals are a smaller,
cash-surplus-funded addition rather than an upfront capital dump. This
replaced an earlier all-crop version and a since-abandoned all-animal
("full pivot") version — see "Design history" below for why.

- **Land split**: `CROP_LAND_SHARE = 0.70` of unlocked tiles go to crops,
  the rest to livestock structures. Within the crop budget, wheat/carrot/
  tomato/melon split 30/25/30/15 by `allocation_ratio` (`_allocate_by_ratio`,
  largest-remainder rounding). Strawberry is deferred — same capital-
  intensive, glut-sensitive profile as tomato/melon without a distinct
  risk/return trade-off yet.
- **Wheat is dual-purpose**: it's a normal cash crop, but its target also
  gets bumped up to cover animal feed demand (`ceil(animal_count * 1.5)`
  tiles) if that exceeds its ratio share — a smaller tomato patch is a much
  better trade than a starving herd.
- **Animals are funded from cash surplus, not the starting bank**:
  `BUY_ANIMAL` only spends money above `ANIMAL_CASH_RESERVE` (1200), each
  purchase's effective cost also reserves `FEED_DAYS_BUFFER` (3) days of
  wheat at the current price, and purchases are capped at
  `MAX_NEW_ANIMALS_PER_TURN` (2). Cow gets the largest share of the
  livestock budget (best $/day and payback), then sheep, then goose.
- **Getting an animal from shed to structure** is a two-location problem
  (`BUY_ANIMAL` delivers to the shed; the animal must be `PICKUP`ed, walked
  to a matching empty `PASTURE`/`COOP`, then `PLACE`d) that a single-position
  `Task` can't express. `_logistics_actions` runs a stateful pass per unit,
  before normal task assignment, for three carry-then-act flows: animal
  delivery, wheat-for-feeding, and fertilizer-for-wheat.
- **Selling is price-aware per product** (`sell_threshold_ratio * base_price`,
  tuned to each product's glut sensitivity), with wheat additionally
  reserving a feed buffer before any surplus is offered, and melon/wool
  capped at 5 units/turn outside the end-game liquidation window.
- Hires up to 6 hands per day, retrying every hour (not just hour 0) so a
  crowded order budget on one hour doesn't leave the farm hand-less all day.
- Buys land once the current quadrant's crop+structure targets are
  essentially filled and cash exceeds the cost plus a reserve.

This is a stronger operational strategy, not a finished tournament strategy.
It does not yet respond to shop-specific demand, and the crop/livestock
land split and cash-reserve constants are hand-picked, not tuned from a
sweep.

## Bugs found and fixed this session

- **One-time crop harvest timing**: crops harvested as soon as
  `age >= first_yield_day and watered_today`, not after the bonus window
  closed at `max_yield_day` — wheat was harvesting at day 2 with 2 units
  instead of waiting to day 4 for 4, throwing away half its yield every
  cycle. Fixed to wait for the bonus window to close (or the hard
  `max_yield` cap to be hit early, which melon already does naturally).
- **`HIRE` hour-0-only gate**: with up to 4 crops + 3 animal products all
  potentially selling simultaneously, hour 0's order budget could be fully
  consumed by `SELL`/`BUY_SEED` before `HIRE` got a slot — with no retry,
  that meant zero hands for the *entire day*. Fixed by retrying every hour
  instead of only hour 0.
- **Redundant wheat/fertilizer pickups**: the logistics pass computed "does
  anyone still need feeding" once per turn but never reduced it after a
  unit queued a `PICKUP`, so every idle unit independently fetched wheat for
  the same handful of animals (37 pickups observed for 6 animals in one
  day). Fixed by removing claimed animals/tiles from the shared list as
  soon as a pickup is queued, not just when the actual `FEED`/`FERTILIZE`
  happens.

## Design history: why hybrid, not a full crop-to-animal pivot

`_decay_plants` in the installed `kaggriculture.py` only ever acts on
`kind == "PLANT"` tiles — animal structures are never subject to decay.
Once bought and fed, an animal produces indefinitely; a crop always dies
after its yield cap and must be replanted. That asymmetry is why an
opponent encountered on the live ladder beat this agent 90k-to-20k running
0 crops and 18 animals (see the ranked-episode analysis below).

A first attempt fully pivoted to wheat-plus-livestock only (no
carrot/tomato/melon). It technically worked (no crashes) but scored
**$4,859** over a full season in local testing — far below the prior
all-crop baseline — because bootstrapping an animal population from a fixed
$3,000 starting bank is fragile: animals escape after 2 unfed days, but
wheat (even before considering the harvest-timing fix) takes 2-4+ days to
first yield. Spending most of the starting cash on animals immediately left
nothing to cover feed until self-grown wheat caught up, causing a die-off
around day 7-9 before the economy recovered. Three safety layers (a wheat
buffer gate before placing a new animal, a feed-cost-aware effective price
per animal, and a per-turn purchase cap) were not enough to fix this while
crops were zeroed out.

Switching to the hybrid model — crops remain the primary, immediately
productive land use, and animals are funded only from cash *surplus* above
a reserve — fixed this without needing to touch the safety layers: crop
revenue arrives independently of the animal population, so there's no
scenario where cash and feed are simultaneously exhausted by the animal
ramp-up.

## Verified local behavior

Verified against `kaggle-environments 1.32.3`:

- `obs["step"]` is present.
- A newly planted, unwatered crop becomes a weed at that same day's refresh:
  planting-day watering is mandatory.
- Each surviving animal makes fertilizer available at end of day.
- The current implementation banks **1** care bonus when an animal was fed and
  cared for, despite the public description saying `2`.
- `BUY_ANIMAL` and `BUY_SEED` deliver to the shed, not directly to a tile or
  the acting unit's inventory; animals additionally require `PICKUP` +
  walking + `PLACE` since a shed pickup only fills inventory, not a tile.

## Benchmarks

All games used the default 720-turn season, seeds 100–109, and `main.agent` as
player 0:

| Version | Opponent | Record | Agent mean bank | Opponent mean bank |
| --- | --- | ---: | ---: | ---: |
| Carrot-only | `starter` | 10W-0L-0T | $7,847.2 | $3,329.2 |
| Carrot-only | `random` | 10W-0L-0T | $7,232.2 | $52.0 |
| Multi-crop | `starter` | 10W-0L-0T | $20,315.8 | $3,460.7 |
| Multi-crop | `random` | 10W-0L-0T | $20,152.7 | $3.0 |
| Full animal pivot | `pass` (single run) | win | $4,859.0 | $10.0 |
| Hybrid crop+animal | `starter` | 10W-0L-0T | $16,806.8 | $3,501.3 |
| Hybrid crop+animal | `random` | 10W-0L-0T | $16,563.9 | $0.0 |

Self-play (seed 314, full season) finished without error: $22,138 vs $7,783
— a reminder that outcomes are noisy even between identical agents, since
both compete for the same shared market.

The hybrid is a large improvement over the broken full pivot but currently
trails the pure multi-crop version on mean bank, despite winning every
benchmark game. `CROP_LAND_SHARE`/`ANIMAL_CASH_RESERVE` were spot-tuned
(0.70/1200 outperformed 0.55/800 in a head-to-head rerun) rather than swept,
so there's likely a better setting; the animal population also only reaches
~6-8 head by day 30 in these runs, well short of the ~18 the opponent below
had, so its indefinite-production upside is still mostly unrealized within
one season. These are local baselines only; live ladder performance depends
on the opponents actually faced.

## Live ladder result (informational, not a local benchmark)

The multi-crop version was submitted and played one ranked episode before
this session's work began: **$20,516 vs an opponent's $90,284**. The
opponent ran 0 crops, 8 cows, 9 sheep, 1 goose, with land bought into `NE`
and 11 hired hands — the direct motivation for this session's animal work.
This result predates the hybrid version above and has not been re-measured
against that specific opponent.

## Next strategy upgrades

1. Sweep `CROP_LAND_SHARE`, `ANIMAL_CASH_RESERVE`, `FEED_DAYS_BUFFER`, and
   `MAX_NEW_ANIMALS_PER_TURN` properly (grid or hill-climb over seeded
   benchmarks) instead of the current spot-tuned values.
2. Investigate why the animal population plateaus around 6-8 head by day 30
   — likely the cash-reserve/throttle combination is still conservative
   relative to how fast crop revenue actually accumulates mid-late game.
3. Forecast town consumption and use price-sensitive sale schedules.
4. Revisit strawberry once ongoing-crop economics are validated further.
5. Resubmit to Kaggle once the hybrid is validated further, and compare its
   live ladder rating against the multi-crop version's single data point.
