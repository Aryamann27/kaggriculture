# Handoff: Kaggriculture Agent Project

Read this first. It's the fastest way to get oriented before touching
anything. Written 2026-08-04.

## TL;DR — what's true right now

- **GitHub repo:** `https://github.com/Aryamann27/kaggriculture` (private,
  owner `Aryamann27`). `main` has the shared code + harness; six
  `strategy/*` branches hold parallel experiment results (see below).
- **`main.py` on `main` (the actual Kaggle submission file) is the
  crop+animal *hybrid* strategy** — it is **not** the tournament winner.
  The winning strategy (`strategy/livestock-stack`) has been benchmarked
  and documented but **never merged to `main` or resubmitted to Kaggle**.
  This is the single most important pending action — see "Immediate next
  steps" below.
- **Kaggle:** one submission exists (`55220446`, "Multi-crop v1", uploaded
  2026-08-03, status `COMPLETE`, current public score `400.5`). It reflects
  an even older version of the strategy than what's on `main` now. It has
  played at least one ranked episode and lost 20,516 to 90,284 against an
  all-animal opponent — the direct motivation for everything in this
  session (see `DEVELOPMENT.md`).
- **Local uv venv** at `Kaggriculture/.venv` works; `pyproject.toml` +
  `uv.lock` are committed. GitHub CLI auth is stored in this machine's
  keychain (see "Credentials" below) — you likely don't need to
  re-authenticate.

## Project structure

```
Kaggle/                          <- git repo root (NOT named Kaggriculture)
  Kaggriculture/                  <- everything lives here
    main.py                       <- Kaggle submission entrypoint (single file)
    run_local.py                  <- benchmark runner, supports --agent-path for any worktree
    pyproject.toml / uv.lock      <- uv-managed deps (kaggle-environments, kaggle CLI)
    tests/                        <- unittest suite (run before trusting any change)
    experiments/
      manifest.json               <- strategy IDs, branches, tier definitions
      seeds/{canary,tier1,tier2}.txt   <- fixed seed lists, DO NOT change casually
      run_matrix.py                <- runs tests + benchmarks across worktrees in parallel
      aggregate.py                 <- summarizes JSONL results (mean/median/std/win-rate)
      compare.py                   <- paired seed-by-seed delta vs a baseline strategy
      LEARNINGS.md                 <- full tournament writeup, READ THIS for strategy details
    DEVELOPMENT.md                 <- running history/benchmarks predating the tournament
    AGENTS.md / README.md          <- original competition rules (from Kaggle, do not edit)
    results/                       <- gitignored; JSONL benchmark output lands here
```

## How the game works (quick reference)

Two players, 30-day season (720 turns), shared market. Full rules are in
`AGENTS.md` and `README.md` — read those before changing game logic. The
single most important mechanic, confirmed by reading the installed engine
source (`kaggriculture.py` in the venv): **animals never decay once placed
and fed — only crops do.** An animal is a one-time capital cost with
indefinite payback; a crop always dies after its yield cap and must be
replanted. Almost every strategic decision in this project traces back to
that asymmetry.

## How we got here (context for the next session)

1. Built a wheat-loop → multi-crop agent locally, benchmarked well
   ($20.3k vs `starter`), submitted to Kaggle.
2. Lost a ranked episode 20.5k–90.3k to an opponent running 18 animals and
   0 crops. Investigated, found the never-decay mechanic, tried a full
   crop→animal pivot. **It failed catastrophically ($4,859)** — a
   naively-paced animal ramp starves itself before its own wheat crop
   catches up.
3. Built a **hybrid** (crops as primary funded engine, animals bought only
   from cash surplus) — this fixed the starvation problem and became the
   version currently on `main`/submitted to Kaggle.
4. User asked: "let's try several strategies in parallel and see which
   wins, and whether combining any of them helps." That's the tournament
   documented in `experiments/LEARNINGS.md`.
5. Five isolated strategies were built in parallel worktrees, benchmarked
   through one shared harness on fixed seeds, and the winner
   (`livestock-stack`, a much more rigorously feed-safety-gated animal
   strategy) was identified. A sixth branch tested combining the two best
   candidates — **the combination made things worse**, a useful negative
   result, not a bug.

**Read `experiments/LEARNINGS.md` for the full per-strategy breakdown**
(what each one did, why it over/underperformed, exact verified numbers).
Don't re-derive this from scratch.

## Tournament result summary

| Strategy | Branch | vs `starter` (50 seeds) | Verdict |
| --- | --- | ---: | --- |
| Livestock stack | `strategy/livestock-stack` | **$19,484.6** | **Winner — promote this** |
| Town oracle | `strategy/town-oracle` | $17,995.9 | Strong but high-variance; keep as reference |
| Multi-crop control | `strategy/multicrop-control` | $15,256.3 | Baseline/control |
| Integration attempt | `strategy/integrate-livestock-town` | $16,652.3 | **Discard** — worse than either parent |
| Ongoing fertilizer | `strategy/ongoing-fertilizer` | $7,445.2 | Discard |
| Staple velocity | `strategy/staple-velocity` | $4,507.5 | Discard |
| **Current `main.py`** (hybrid, pre-tournament) | `main` | $16,806.8 | Superseded by `livestock-stack` |

All numbers are verified through the shared harness (not self-reported),
on fixed seeds 100–109 (Tier 1) or 100–149 (Tier 2). See `LEARNINGS.md` for
head-to-head and combination-test details.

## Immediate next steps (pick up here)

1. **Decide whether to promote `strategy/livestock-stack` to `main.py` and
   resubmit to Kaggle.** It's the clear local winner but has a modest final
   herd (~3 cows) — it hasn't reproduced the ~18-animal scale of the
   live-ladder opponent that started this investigation. Worth asking the
   user whether to promote as-is or push further first.
2. **If promoting:** copy `strategy/livestock-stack`'s `main.py` (commit
   `414356c`) onto `main`, re-run the full test suite +
   `run_local.py --opponent starter --episodes 10` to reconfirm, update
   `DEVELOPMENT.md`, then submit via
   `kaggle competitions submit kaggriculture -f main.py -m "<message>"`
   (5/day limit, only latest 2 submissions stay active/matched).
3. **`livestock-stack`'s herd cap is a known, flagged limitation** — see
   `LEARNINGS.md`'s caveats section. If asked to push further, look there
   first: the safety gates (wheat bootstrap floor, feed-buffer sizing,
   structure-slot cap, per-turn purchase throttle, cash floor) are
   deliberately conservative and are the reason it's robust; relaxing them
   without care risks reproducing the earlier $4,859 starvation failure.
4. **Local worktree cleanup:** five Cursor-managed worktrees still exist
   under `.cursor/worktrees/` (one per strategy). Everything in them is
   already committed and pushed to GitHub, so they're safe to remove with
   `/delete-worktree` if disk space matters. Not urgent.

## How to run things

```bash
cd Kaggriculture

# Tests
uv run python -m unittest discover -s tests -v

# Benchmark the current main.py
uv run python run_local.py --opponent starter --episodes 10 --seed 100
uv run python run_local.py --opponent random --episodes 10 --seed 100

# Benchmark an arbitrary strategy worktree against a fixed seed set
uv run python run_local.py --agent-path /path/to/other/main.py \
  --seed-file experiments/seeds/tier1.txt --opponent starter \
  --output results/some-run/starter.jsonl --strategy some-name --quiet

# Full tier matrix across several worktrees in parallel
uv run python experiments/run_matrix.py --tier tier1 \
  --worktree name1=/path/to/worktree1 --worktree name2=/path/to/worktree2 \
  --results-dir results --run-id my-run --parallel 2

# Aggregate + paired comparison
uv run python experiments/aggregate.py --run-id my-run
uv run python experiments/compare.py --run-id my-run --baseline name1
```

`run_local.py --agent-path` dynamically loads any `main.py` by file path —
this is how every strategy in the tournament was benchmarked identically
without duplicating the harness into each branch. If a candidate uses
`@dataclass`, make sure you're on a `run_local.py` with the loader fix
(commit `18f9de6` or later on `main`) — earlier versions crash on
dataclass-based agents loaded this way.

## Credentials (do not print secrets; just where to find them)

- **GitHub:** authenticated via a temporary GitHub CLI binary at
  `/tmp/kaggriculture-gh/gh_2.97.0_macOS_arm64/bin/gh` (downloaded and
  checksum-verified from the official `cli/cli` release; not installed
  system-wide). Git pushes in this session used
  `git -c credential.helper='!/tmp/kaggriculture-gh/.../gh auth git-credential' push ...`
  rather than modifying global git config. The `/tmp` binary may not
  survive a reboot — if pushes fail with "could not read Username", re-run
  `gh auth login --hostname github.com --git-protocol https --web` (with
  `gh` installed via your own means) or just ask the user to re-auth.
- **Kaggle:** API token lives at `~/.kaggle/access_token` (already set up,
  should not need to be redone). Verify with
  `kaggle competitions submissions kaggriculture`.
- Both were set up interactively with the user earlier in this project;
  don't try to generate new tokens unless these stop working.

## Things that will bite you if you don't know them

- **Harvest timing bug (fixed, but easy to reintroduce):** one-time crops
  (wheat, carrot) must wait until `age > max_yield_day` or the hard
  `max_yield` cap is hit before harvesting — harvesting as soon as
  `age >= first_yield_day` throws away the entire bonus-watering window.
  Every strategy branch has this fix; if you write new crop logic from
  scratch, don't lose it.
- **`BUY_ANIMAL`/`BUY_SEED` land in the shed, not a tile or inventory.**
  Placing an animal or feeding one requires a separate `PICKUP` +
  walk + `PLACE`/`FEED` sequence. All five strategies implement this as a
  stateful "logistics" pass that runs before the normal per-tile task
  assignment.
- **`HIRE` must retry every hour, not just hour 0** — with several
  products all wanting to sell/buy in the same turn, hour 0's 10-order
  budget can be completely consumed before `HIRE` gets a slot, leaving the
  farm hand-less for the entire day if there's no retry.
- **The engine's `CARE` action banks a `+1` bonus, not the `+2` the public
  README describes.** Trust the installed engine source over the docs if
  they disagree — `kaggriculture.py` under the venv's
  `site-packages/kaggle_environments/envs/kaggriculture/` is the source of
  truth.
- **Sandbox note:** shell commands run outside this workspace's root
  (e.g. sibling worktree folders, `/tmp`, `~/.kaggle`) may silently fail to
  apply `working_directory`/write permissions unless you request broader
  permissions explicitly. If a command "succeeds" but touches the wrong
  files, verify `pwd` and file hashes before trusting output — this
  happened once this session and wasted a debugging cycle.
