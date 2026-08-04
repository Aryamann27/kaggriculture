"""Run reproducible local Kaggriculture benchmarks.

The runner deliberately separates the benchmark harness from a strategy:
``--agent-path`` dynamically loads any worktree's ``main.py`` so every
candidate can be evaluated under identical seeds/opponents without copying
the harness into its branch.

Examples:
    uv run python run_local.py --opponent starter --episodes 20
    uv run python run_local.py --agent-path ../worktrees/town-oracle/main.py \
        --seed-file experiments/seeds/tier1.txt --opponent starter \
        --output results/town-oracle/starter.jsonl
    uv run python run_local.py --agent-path ../worktrees/a/main.py \
        --opponent-agent ../worktrees/b/main.py --seed-file experiments/seeds/tier2.txt
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from statistics import mean
from typing import Any

from kaggle_environments import make


Agent = Callable[[dict[str, Any]], dict[str, Any]]
BUILT_IN_OPPONENTS = ("pass", "random", "starter")


def load_agent(agent_path: Path) -> Agent:
    """Load an ``agent(obs)`` function from an arbitrary worktree file."""
    resolved = agent_path.expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"Agent file not found: {resolved}")

    module_name = f"kaggriculture_agent_{abs(hash(resolved))}"
    spec = importlib.util.spec_from_file_location(module_name, resolved)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load agent module from {resolved}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    candidate = getattr(module, "agent", None)
    if not callable(candidate):
        raise TypeError(f"{resolved} must define a callable agent(obs)")
    return candidate


def read_seeds(seed_file: Path | None, seed: int, episodes: int) -> list[int]:
    """Read one integer seed per non-empty line, or create a sequential range."""
    if seed_file is None:
        if episodes <= 0:
            raise ValueError("--episodes must be positive")
        return [seed + offset for offset in range(episodes)]

    values: list[int] = []
    for line in seed_file.expanduser().read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        values.append(int(text))
    if not values:
        raise ValueError(f"No seeds found in {seed_file}")
    return values


def git_sha(path: Path) -> str | None:
    """Return the strategy worktree's current SHA without requiring Git."""
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=path.parent,
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark a Kaggriculture agent locally.")
    parser.add_argument(
        "--agent-path",
        type=Path,
        default=Path(__file__).with_name("main.py"),
        help="Path to the candidate main.py (default: this worktree's main.py).",
    )
    parser.add_argument(
        "--opponent",
        choices=BUILT_IN_OPPONENTS,
        default="starter",
        help="Built-in opponent, ignored when --opponent-agent is supplied.",
    )
    parser.add_argument(
        "--opponent-agent",
        type=Path,
        help="Path to a second custom agent for head-to-head or self-play.",
    )
    parser.add_argument(
        "--episodes",
        type=int,
        default=10,
        help="Number of sequential seeds when --seed-file is not supplied.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=0,
        help="First sequential seed when --seed-file is not supplied.",
    )
    parser.add_argument(
        "--seed-file",
        type=Path,
        help="Text file with one fixed integer seed per line.",
    )
    parser.add_argument(
        "--steps",
        type=int,
        default=720,
        help="Episode length in turns.",
    )
    parser.add_argument(
        "--replay-dir",
        type=Path,
        help="Optional directory in which to save replay JSON files.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional JSONL file to append one structured row per episode.",
    )
    parser.add_argument(
        "--run-id",
        default="manual",
        help="Experiment identifier attached to structured result rows.",
    )
    parser.add_argument(
        "--strategy",
        default="local",
        help="Strategy identifier attached to structured result rows.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress per-episode stdout (structured output still writes).",
    )
    return parser.parse_args()


def run_episode(
    candidate: Agent,
    opponent: str | Agent,
    seed: int,
    steps: int,
) -> tuple[float, float, str, str, dict[str, Any]]:
    """Run one game and return rewards, statuses, and the replay payload."""
    env = make(
        "kaggriculture",
        configuration={"episodeSteps": steps, "seed": seed},
        debug=True,
    )
    env.run([candidate, opponent])
    final = env.steps[-1]
    candidate_state, opponent_state = final[0], final[1]
    return (
        float(candidate_state.reward or 0),
        float(opponent_state.reward or 0),
        str(candidate_state.status),
        str(opponent_state.status),
        env.toJSON(),
    )


def outcome_for(candidate_reward: float, opponent_reward: float) -> str:
    if candidate_reward > opponent_reward:
        return "win"
    if candidate_reward < opponent_reward:
        return "loss"
    return "tie"


def append_result(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")


def main() -> None:
    args = parse_args()
    if args.steps <= 0:
        raise SystemExit("--steps must be positive")

    candidate_path = args.agent_path.expanduser().resolve()
    candidate = load_agent(candidate_path)
    opponent: str | Agent = (
        load_agent(args.opponent_agent) if args.opponent_agent else args.opponent
    )
    opponent_label = (
        str(args.opponent_agent.expanduser().resolve())
        if args.opponent_agent
        else args.opponent
    )
    seeds = read_seeds(args.seed_file, args.seed, args.episodes)

    if args.replay_dir:
        args.replay_dir.mkdir(parents=True, exist_ok=True)
    candidate_sha = git_sha(candidate_path)
    results: list[tuple[float, float]] = []

    for seed in seeds:
        started = time.perf_counter()
        error: str | None = None
        replay_path: str | None = None
        try:
            candidate_reward, opponent_reward, candidate_status, opponent_status, replay = run_episode(
                candidate,
                opponent,
                seed,
                args.steps,
            )
        except Exception as exc:  # Record failed candidates for comparison.
            candidate_reward = opponent_reward = 0.0
            candidate_status = opponent_status = "ERROR"
            replay = {}
            error = f"{type(exc).__name__}: {exc}"
        elapsed_seconds = time.perf_counter() - started
        outcome = outcome_for(candidate_reward, opponent_reward)
        results.append((candidate_reward, opponent_reward))

        if args.replay_dir and error is None:
            replay_file = args.replay_dir / f"{args.strategy}-{seed}.json"
            replay_file.write_text(json.dumps(replay), encoding="utf-8")
            replay_path = str(replay_file)

        row = {
            "run_id": args.run_id,
            "strategy": args.strategy,
            "git_sha": candidate_sha,
            "agent_path": str(candidate_path),
            "opponent": opponent_label,
            "seed": seed,
            "steps": args.steps,
            "agent_reward": candidate_reward,
            "opponent_reward": opponent_reward,
            "margin": candidate_reward - opponent_reward,
            "outcome": outcome,
            "agent_status": candidate_status,
            "opponent_status": opponent_status,
            "duration_seconds": round(elapsed_seconds, 4),
            "replay_path": replay_path,
            "error": error,
        }
        if args.output:
            append_result(args.output, row)

        if not args.quiet:
            suffix = f" error={error}" if error else ""
            print(
                f"seed={seed} result={outcome:4} "
                f"agent=${candidate_reward:.0f} opponent=${opponent_reward:.0f}{suffix}"
            )

    wins = sum(candidate_reward > opponent_reward for candidate_reward, opponent_reward in results)
    losses = sum(candidate_reward < opponent_reward for candidate_reward, opponent_reward in results)
    ties = len(results) - wins - losses
    print(
        "\nSummary: "
        f"{wins}W-{losses}L-{ties}T | "
        f"agent mean=${mean(value for value, _ in results):.1f} | "
        f"opponent mean=${mean(value for _, value in results):.1f}"
    )


if __name__ == "__main__":
    main()
