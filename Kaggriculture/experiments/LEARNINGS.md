# Parallel Strategy Tournament — Learnings

Five strategy hypotheses were implemented in isolated Git worktrees from the
same base commit (`8b54fcb`), benchmarked through one shared harness
(`run_local.py` + `experiments/run_matrix.py`) on identical fixed seeds, then
the top two were taken to a head-to-head decider and a combination was
empirically tested. All numbers below are verified through the shared
harness (not self-reported subagent output) unless stated otherwise.

## Results at a glance

Tier 1 (seeds 100–109, full 720-turn season), all vs `starter`/`random`:

| Strategy | vs `starter` | vs `random` | Std dev (`starter`) |
| --- | ---: | ---: | ---: |
| `staple-velocity` | $4,507.5 | $4,645.7 | $479.0 |
| `ongoing-fertilizer` | $7,445.2 | $6,591.6 | $972.2 |
| `multicrop-control` | $15,278.5 | $15,287.2 | $287.3 |
| `town-oracle` | $17,197.5 | $18,151.1 | $3,272.3 |
| `livestock-stack` | $19,335.7 | $19,216.8 | $811.1 |

Tier 2 (seeds 100–149, promotion set) for the two finalists plus the control:

| Strategy | vs `starter` | Std dev | Paired vs `livestock-stack` |
| --- | ---: | ---: | --- |
| `multicrop-control` | $15,256.3 | $391.2 | -$4,228.4 mean, 0/50 seeds better |
| `town-oracle` | $17,995.9 | $2,918.7 | -$2,739.6 mean, 40/50 seeds worse |
| `livestock-stack` | $19,484.6 | $1,696.4 | baseline |

Head-to-head, `livestock-stack` vs `town-oracle`, both seat assignments
(seeds 100–149):

| Seat | Record | Mean bank |
| --- | --- | --- |
| `livestock-stack` as player 0 | 26W–24L | $17,876.9 vs $17,402.5 |
| `town-oracle` as player 0 | 26W–24L (i.e. `livestock-stack` 24W–26L) | $17,158.3 vs $17,961.0 |
| **Combined (order-corrected)** | **50–50 tie** | `livestock-stack` $17,919 vs `town-oracle` $17,280 |

Combination test, `strategy/integrate-livestock-town` (town's demand-driven
cash-crop allocation + sell timing grafted onto `livestock-stack`'s engine,
its feed-safety gates left untouched):

| Strategy | vs `starter` (50 seeds) | Paired vs `livestock-stack` |
| --- | ---: | --- |
| `integrate-livestock-town` | $16,652.3 | **-$2,832.3 mean, 45/50 seeds worse** |

**The combination made things worse, not better.** This is the key
falsified hypothesis of the whole exercise — see the dedicated section below.

## Per-strategy findings

### `multicrop-control` — pure crop portfolio (control)

**Approach:** WHEAT/CARROT/TOMATO/MELON at a static 30/25/30/15 tile-ratio
split, price-aware selling with per-product glut thresholds, six daily
hands, the corrected one-time-crop harvest timing (wait for the bonus
window/hard cap, not `first_yield_day`).

**Advantages:**
- Lowest variance of any candidate (σ ≈ $287–391) — no feed-safety
  machinery, no starvation risk, nothing that depends on a random shop
  unlock sequence.
- Simple to reason about and cheap to verify; a good reference point for
  everything else.

**Caveats / why it's not the ceiling:**
- Every tile is a *repeating* cost: crops always decay past `max_yield`
  and must be replanted, so income is capped by continuous
  plant/water/harvest/replant labor throughput rather than by accumulating
  capital.
- Its own implementer found that adding land expansion on top of this
  ratio *reduced* the mean from ~$15.3k to ~$12.0k and disabled it —
  suggesting the crop-only economy doesn't have a good use for the
  incremental tiles land buys, at least not with a static ratio.
- Notably scored below the $20,315.8 historical multi-crop baseline
  recorded earlier this session under a different (pre-tournament)
  implementation. The exact allocation/threshold constants differ between
  that iteration and this from-scratch rebuild; this discrepancy is flagged
  as an open question rather than fully explained — worth a future
  side-by-side diff if the crop-only ceiling matters again.

### `staple-velocity` — wheat/carrot only

**Approach:** Only two fast-cycling one-time crops, no tomato/melon/animals,
price-throttled carrot sales, cash/saturation-gated NE expansion.

**Result: worst of all five ($4,507.5 vs `starter`).**

**Why it underperformed everything:** Dropping to two crops sacrifices
tomato's ongoing high-value production and melon's high per-unit price
entirely, and with no animals or (apparently) aggressive-enough land/hand
scaling, there's no other lever to compensate. Concentrating volume into
just two products also means gluts hit harder per-crop than in a five-way
split. The hypothesis "fast payback beats slow premium/animal paths" was
not supported — payback speed didn't matter as much as having more
distinct income lines to grow into.

### `ongoing-fertilizer` — tomato + goose pipeline

**Approach:** Tomato as the core ongoing crop, a capped 2-goose flock for
egg income and fertilizer, fertilizer routed to watered tomatoes on
production days, optional strawberry only when justified.

**Result: second-worst ($7,445.2 vs `starter`).**

**Why it underperformed:** The fertilizer/goose mechanics themselves worked
as designed (13 fertilizer applications, 16 collections, 35 feeds, no
duplicate purchases in the seed-11 trace) — this was an operational
success, not a bug. But the *economic* result was a smaller, less
diversified version of `multicrop-control` (fewer crop types, no wheat/
carrot/melon revenue) that also didn't capture livestock's indefinite-
production upside (only 2 geese, deliberately capped). It inherited crop
decay's downside without gaining animals' compounding upside. Its Tier 0
self-play canary was also 0W–2L, a weak signal on its own but consistent
with the broader pattern of underperformance.

### `town-oracle` — demand-adaptive strategy

**Approach:** Reads `obs["town"]["unlocked_shops"]`, scores real (not
predicted) product demand, shifts crop/animal targets toward demanded
products, times sales to just after town consumption drains inventory
(so re-selling doesn't refill the market before the town could drain it),
and caps premium products to the batch size the town just consumed.

**Advantages:**
- Second-best mean ($17,197.5–$17,995.9) using only crops plus a
  cash-surplus-funded light animal presence — no dedicated feed
  infrastructure.
- The underlying idea is sound: known demand is a real, exploitable signal,
  and this candidate never speculates on shops that haven't unlocked yet.

**Caveats:**
- By far the highest variance of the strong candidates (σ ≈ $2,918–3,462,
  roughly 2–4x `livestock-stack`'s). Performance depends on the *random*
  shop-unlock sequence each seed draws — a seed that front-loads
  favorable shops scores much higher than one that doesn't.
- In direct head-to-head against `livestock-stack` it's a genuine 50/50
  coin flip (50 wins each across both seat orders) despite scoring lower
  against `starter`/`random` on average — its adaptability doesn't clearly
  dominate an animal-centric approach once both are competing for the same
  shared market rather than against a static baseline.
- For competitive ladder play, high variance is a real cost independent of
  mean: a strategy that occasionally does much worse is riskier than one
  with a similar or slightly lower mean but tighter spread.

### `livestock-stack` — cow-led feed-safe animal stack (winner)

**Approach:** Reserves 30% of land for livestock and an equal-sized
*dedicated* wheat patch (not shared with any cash-crop logic), and gates
every single animal purchase/placement behind multiple independent safety
checks: a wheat-bootstrap floor before *any* animal is bought, a full
multi-day feed-buffer requirement sized to the *committed* herd (not a
shared minimum), a structure-slot cap, a per-turn purchase throttle, and a
cash floor that always retains enough to refill the feed buffer once.
Early crop revenue funds NE expansion; NE then funds a bigger herd. Milk
and wool sell only in small batches once a worthwhile price is reached.

**Result: best of all five and of the finalist pair ($19,335.7–$19,484.6
vs `starter`), and the *only* candidate to win 100% of paired seeds against
the crop-only control (Tier 1 and Tier 2, 60 seeds combined).**

**Advantages:**
- Directly validates this session's core thesis — animals never decay, so
  each purchased head is a one-time capital cost with indefinite payback —
  while avoiding the catastrophic failure mode from *before* this
  tournament, where an unsafely-paced "full pivot" to animals scored only
  $4,859 due to a starvation death spiral. The difference between $4,859
  and $19,484.6 is entirely attributable to feed-safety engineering, not a
  different economic thesis.
- Zero animal escapes observed across every verified seed in both tiers.
- Lower variance than `town-oracle` (σ ≈ $1,696–1,814) despite a higher
  mean — the more attractive candidate on a risk-adjusted basis, not just
  a raw-mean basis.

**Caveats:**
- Its final herd is modest (commonly ~3 cows, 0 sheep/geese in verification
  runs) — it has *not* reproduced the live-ladder opponent's ~18-animal
  scale. The safety gates are proven conservative; there is real headroom
  if they can be relaxed adaptively as cash reserves grow, without
  reintroducing the starvation risk.
- Cash crops here are a secondary, smaller funding source (`CARROT`/
  `TOMATO`/`MELON` only — all wheat is feed-dedicated, none is sold as a
  cash crop). It trades away the crop-only ceiling on the staple side for
  the animal compounding upside; on this 30-day horizon that trade pays
  off, but it's a real trade, not a free upgrade.

### `integrate-livestock-town` — combination test (discarded)

**Approach:** Took `livestock-stack` as the base (all feed-safety gates,
wheat bootstrap, and animal-purchase logic left byte-for-byte untouched)
and grafted on `town-oracle`'s demand module, scoped *only* to selecting
among `CARROT`/`TOMATO`/`MELON` and timing their sales around town-drain
windows.

**Result: worse than `livestock-stack` alone on 45 of 50 paired seeds
(-$2,832.3 mean), and not clearly better than `town-oracle` alone either.**

**Why the combination failed — the key learning:** `livestock-stack`'s
original static 45/35/20 cash-crop ratio and its "sell once price clears
threshold" rule were tuned to convert crop revenue into NE-unlock and
first-cow funding as *fast* as possible — that pipeline has its own
implicit timing requirements. Grafting on `town-oracle`'s demand-driven
crop mix (which shifts toward whatever's currently demanded, uncorrelated
with `livestock-stack`'s funding needs) and its "wait for the post-town-
drain window" sell-timing gate (deliberately *delays* sales) actively
worked against those requirements instead of complementing them. Two
components that each look strong in isolation — one for crop-only mean
return, one for demand-adaptive selling — do not automatically compose:
the receiving strategy's internal economy had different speed and timing
needs than the donor module was designed to serve, and this was
empirically confirmed, not just theorized.

## Overall conclusions

1. **Feed-safety engineering, not the animal thesis itself, was the
   decisive variable.** The same "animals compound" idea produced a
   catastrophic $4,859 (unsafe pacing, pre-tournament) and the tournament's
   best result at $19,484.6 (rigorously gated). Any future animal-based
   work should keep `livestock-stack`'s layered gates (wheat bootstrap,
   full feed-buffer sizing, structure-slot cap, per-turn throttle, cash
   floor) as a starting point, not simplify them.
2. **Diversification matters for crop-only strategies.** Both
   underperforming candidates (`staple-velocity`, `ongoing-fertilizer`)
   used fewer distinct products than `multicrop-control`; both scored
   well below it.
3. **Variance is a real, separate axis from mean return.** `town-oracle`'s
   demand-adaptiveness produced a strong mean but 2–4x the variance of
   `livestock-stack`, and a genuine coin-flip head-to-head despite a lower
   solo mean — a reminder that mean-vs-`starter` alone is an incomplete
   promotion criterion for adversarial ladder play.
4. **Combining two winning strategies is not automatically additive, and
   can actively hurt.** This was tested empirically (not assumed): the
   graft of `town-oracle`'s demand module onto `livestock-stack` made
   results worse across 90% of paired seeds. Future combination attempts
   should validate that a donor module's implicit timing/funding
   assumptions are compatible with the base strategy's, not just that both
   parents individually beat a common baseline.

## Recommendation

Promote `livestock-stack` (`strategy/livestock-stack`, commit `414356c`) as
the strongest, most robust candidate: best mean return, lowest variance
among the top performers, zero safety failures across every verified seed,
and a 100% paired win rate against the crop-only control. Discard
`staple-velocity`, `ongoing-fertilizer`, and `integrate-livestock-town`.
Keep `town-oracle` as a documented, working alternative — its demand-
adaptive selling logic remains a candidate for a *different*, more
carefully-scoped integration attempt in the future, but not the one tested
here.
